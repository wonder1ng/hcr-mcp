"""공고 URL/스크린샷 → 구조화 JSON(JobPosting). jobs[] 분리 규칙, headcount/deadline 보존
규칙 등은 HcR/hiring_preprocess/clean_all_jobs.py 패턴 이식(자세한 프롬프트 규칙은
prompts.py 참고). fit/job_collector.py(자유 텍스트 반환) 재작성.

우선순위: URL 스크래핑 시도 → 실패/내용 부족 시 스크린샷 비전 추출. 스크래핑/추출 직후,
LLM 정규화 이전에 원문을 storage.save_raw로 즉시 저장(정규화가 실패해도 원문은 남는다 —
report_builder.collect_and_save_news와 동일 패턴).

잡코리아(GI_Read) URL은 일반 텍스트 추출만으로는 사이드바 요약 카드(모집분야/급여/근무지 등)만
잡히고 실제 상세 본문(담당업무/자격요건/우대사항/제출서류)은 비어서 나온다 — 최신 잡코리아
페이지가 Next.js CSR이라 상세 본문을 브라우저에서 별도 API로 불러오기 때문(정적 HTML에 아예
없음, 실측 확인). 대신 잡코리아는 상세 본문을 별도의 가벼운 iframe 콘텐츠 URL
(`GI_Read_Comt_Ifrm?Gno={id}`)로도 서빙하는데, 이 URL은 정적 HTML이라 일반 fetch로 그대로
가져와진다 — `HcR/scrapy/jobkoreaScrapy.py::scrapy_loop`가 이미 이 URL 변형 트릭을 쓰고 있어
그대로 이식(`_jobkorea_detail_url`). 사이드바 요약 + 이 상세 본문 둘 다 있어야 완전한 정보라
두 소스를 모두 모은다.
"""

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from hcr_mcp import llm_client
from hcr_mcp.job_posting.prompts import JOB_POSTING_HUMAN, JOB_POSTING_SYSTEM
from hcr_mcp.job_posting.schemas import JobPosting
from hcr_mcp.storage import Storage
from hcr_mcp.web_fetch import fetch_page_text

logger = logging.getLogger("hcr_mcp.job_posting.collector")

_MIN_USEFUL_CHARS = 200  # 이보다 짧으면 JS 렌더링/차단으로 보고 스크린샷 폴백을 권장
_DEADLINE_RE = re.compile(r"(20\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})")
_JOBKOREA_HOSTS = {"jobkorea.co.kr", "www.jobkorea.co.kr"}
_JOBKOREA_GI_READ_PATH_RE = re.compile(r"^/Recruit/GI_Read/(\d+)$")


def _jobkorea_detail_url(url: str) -> str | None:
    """잡코리아 공고 URL(호스트+경로 형식을 정확히 검증한 뒤)이면 상세 본문 iframe URL을,
    아니면 None을 반환한다. 호스트는 정확히 일치해야 함(예: "evil-jobkorea.co.kr.attacker.com"
    같은 값이 부분 문자열 매칭으로 통과하지 않도록) — 쿼리스트링(?Oem_Code=... 등)은 경로 검증과
    무관하므로 자연히 무시된다."""
    parsed = urlparse(url)
    if parsed.hostname not in _JOBKOREA_HOSTS:
        return None
    m = _JOBKOREA_GI_READ_PATH_RE.match(parsed.path)
    return f"https://www.jobkorea.co.kr/Recruit/GI_Read_Comt_Ifrm?Gno={m.group(1)}" if m else None


async def collect_job_posting(
    job_title: str,
    storage: Storage,
    url: str | None = None,
    screenshot_paths: list[str | Path] | None = None,
) -> JobPosting:
    """공고 원문을 스크래핑/비전 추출로 모아 구조화한다. URL 스크래핑과 스크린샷 중
    아무것도 성공하지 못해도 job_title만으로 최소한의 구조를 반환한다(하드 실패 없음)."""
    parts = []
    vision_used = False

    if url:
        scraped = await fetch_page_text(url)
        if scraped and len(scraped) >= _MIN_USEFUL_CHARS:
            parts.append(f"[공고 원문 (URL: {url})]\n{scraped}")

        detail_url = _jobkorea_detail_url(url)
        detail_text = await fetch_page_text(detail_url) if detail_url else None
        if detail_text:
            parts.append(f"[공고 상세 본문 (URL: {detail_url})]\n{detail_text}")

        if not parts:
            logger.info("URL 스크래핑 결과가 부족합니다. 스크린샷 입력이 있으면 그것으로 보완합니다.")

    if screenshot_paths:
        images = [Path(p).read_bytes() for p in screenshot_paths]
        extracted = await llm_client.vision_extract(
            images, "이 스크린샷들은 채용 공고 화면입니다. 화면에 보이는 공고 내용을 빠짐없이 텍스트로 옮겨 적어주세요."
        )
        parts.append(f"[스크린샷에서 추출한 공고 내용]\n{extracted}")
        vision_used = True

    posting_text = "\n\n".join(parts)
    storage.save_raw("job_posting", job_title, "raw_text.txt", posting_text.encode("utf-8"))

    chain = llm_client.structured_chain(JOB_POSTING_SYSTEM, JOB_POSTING_HUMAN, JobPosting)
    posting: JobPosting = await llm_client.safe_ainvoke(chain, {"job_title": job_title, "posting_text": posting_text})

    posting.raw_meta.source_url = url
    posting.raw_meta.vision_used = vision_used
    posting.raw_text = posting_text
    _apply_deadline_fallback(posting, posting_text)
    _warn_identical_tracks(posting)

    storage.save_report("job_posting", job_title, posting.model_dump())
    return posting


def _apply_deadline_fallback(posting: JobPosting, posting_text: str) -> None:
    """LLM이 deadline을 못 채웠을 때 원문에서 날짜 후보를 찾아 채운다(마지막 날짜 사용) —
    clean_all_jobs.py 실측으로 확인된 흔한 누락 케이스, 추가 LLM 호출 없이 정규식으로 보강."""
    if posting.work_conditions.deadline:
        return
    matches = _DEADLINE_RE.findall(posting_text)
    if not matches:
        return
    year, month, day = matches[-1]
    posting.work_conditions.deadline = f"{year}-{int(month):02d}-{int(day):02d}"


def _warn_identical_tracks(posting: JobPosting) -> None:
    """newcomer/experienced 조건이 완전히 동일하면(원공고에 트랙 구분이 없었을 가능성) 경고를
    남긴다 — clean_all_jobs.py에서 실측으로 확인된 LLM 오분류 신호."""
    for job in posting.jobs:
        tracks = job.tracks
        if not tracks or not tracks.newcomer or not tracks.experienced:
            continue
        n, e = tracks.newcomer, tracks.experienced
        identical = (
            n.requirements == e.requirements
            and n.preferred == e.preferred
            and n.responsibilities == e.responsibilities
            and any([n.requirements, n.preferred, n.responsibilities])
        )
        if identical:
            warning = f"{job.job_name}: newcomer/experienced 조건이 동일 — 원공고 구분 없음 가능성"
            warnings = posting.preprocess_log.parse_warnings or []
            if warning not in warnings:
                warnings.append(warning)
            posting.preprocess_log.parse_warnings = warnings
