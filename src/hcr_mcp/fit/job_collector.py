"""공고 URL/스크린샷 → 자유 텍스트. 신규 빌드 (참고할 기존 스크래퍼 없음).

우선순위: URL 스크래핑 시도 → 실패/내용 부족 시 스크린샷 비전 추출 → 직무명 텍스트는 항상 포함.
사이트별 셀렉터는 없음(일반 텍스트 추출) — 특정 사이트에서 품질이 부족하면 그때 추가.
"""

import logging
from pathlib import Path

from hcr_mcp import llm_client
from hcr_mcp.web_fetch import fetch_page_text

logger = logging.getLogger("hcr_mcp.fit.job_collector")

_MIN_USEFUL_CHARS = 200  # 이보다 짧으면 JS 렌더링/차단으로 보고 스크린샷 폴백을 권장


async def collect_job_posting(
    job_title: str,
    url: str | None = None,
    screenshot_paths: list[str | Path] | None = None,
) -> str:
    """공고 정보를 하나의 자유 텍스트로 합쳐 반환한다 (JobProfile 생성 입력용).

    URL 스크래핑과 스크린샷 중 아무것도 성공하지 못해도 job_title만으로 최소한의 신호는 남긴다(하드 실패 없음).
    """
    parts = [f"[직무명]\n{job_title}"]

    scraped = await fetch_page_text(url) if url else None
    if scraped and len(scraped) >= _MIN_USEFUL_CHARS:
        parts.append(f"[공고 원문 (URL: {url})]\n{scraped}")
    elif url:
        logger.info("URL 스크래핑 결과가 부족합니다(%s자). 스크린샷 입력이 있으면 그것으로 보완합니다.", len(scraped or ""))

    if screenshot_paths:
        images = [Path(p).read_bytes() for p in screenshot_paths]
        extracted = await llm_client.vision_extract(
            images, "이 스크린샷들은 채용 공고 화면입니다. 화면에 보이는 공고 내용을 빠짐없이 텍스트로 옮겨 적어주세요."
        )
        parts.append(f"[스크린샷에서 추출한 공고 내용]\n{extracted}")

    return "\n\n".join(parts)
