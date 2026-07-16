"""채용 사이트 기업정보 수집 — 회사 분석 보고서의 base data (공고 URL이 있는 한 항상 확보 시도).

3단계 폴백:
  1. company_info_url이 주어지면 그 페이지부터 직접 파싱 시도(링크 추출 단계 생략).
  2. job_posting_url에서 기업정보 페이지 링크를 찾아 이동해 파싱.
     (1),(2) 모두 사이트별 파서(site_parsers.py, 잡코리아/게임잡)를 먼저 시도하고,
     인식 못 하는 사이트거나 셀렉터가 안 맞으면 그 페이지 텍스트를 LLM 일반 추출로 보완.
  3. 위 두 가지 모두 실패하면(비공개·차단 등) company_info_screenshot_paths를 비전 LLM으로 추출.
"""

import json
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from hcr_mcp import llm_client
from hcr_mcp.job_posting import site_parsers
from hcr_mcp.web_fetch import fetch_page_html, html_to_text

_LLM_SYSTEM_PROMPT = """당신은 채용/기업 사이트 페이지에서 기업정보만 추출하는 전문가입니다.
페이지 텍스트에는 공고 내용과 기업정보가 섞여 있을 수 있습니다. 기업정보(매출액, 사원수,
대표자, 설립일, 업종, 기업형태, 근무지, 복지, 인재상 등 회사 자체에 대한 정보)만 골라
JSON으로 반환하세요. 공고 내용(직무·자격요건 등)은 무시합니다. 정보가 없으면 null로
표시하세요. 다른 설명 없이 순수 JSON만 출력하세요."""

_JSON_SCHEMA_TEXT = """{
  "revenue": "매출액",
  "employee_count": "사원수",
  "ceo_name": "대표자명",
  "founded": "설립일",
  "industry": "업종",
  "company_type": "기업형태 (예: 중소기업/대기업/외국계 등)",
  "location": "근무지/소재지",
  "benefits": ["복지 항목 1", "복지 항목 2"],
  "talent_values": "인재상"
}"""

_LLM_USER_PROMPT_TEMPLATE = f"""페이지 텍스트:
{{page_text}}

아래 JSON 형식으로 기업정보만 추출해줘(정보 없는 필드는 null, benefits는 없으면 빈 배열):
{_JSON_SCHEMA_TEXT}"""


def _has_any_value(info: dict) -> bool:
    return any(v for v in info.values() if v not in (None, "", []))


async def _llm_fallback_extract(page_text: str) -> dict | None:
    raw = await llm_client.chat(
        [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": _LLM_USER_PROMPT_TEMPLATE.format(page_text=page_text[:6000])},
        ],
        temperature=0,
    )
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return info if _has_any_value(info) else None


def _find_company_link(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")

    # 게임잡 공고 상세페이지의 알려진 회사명 링크
    known = soup.select_one("div.view__header-title div.corp-name a")
    if known and known.get("href"):
        return urljoin(base_url, known["href"])

    # 범용: 텍스트/href에 기업정보 관련 키워드가 있는 링크
    keywords = ["기업정보", "회사소개", "company", "corp"]
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if any(kw in href.lower() or kw in text for kw in keywords):
            return urljoin(base_url, href)
    return None


async def _fetch_and_parse(url: str) -> dict | None:
    html = await fetch_page_html(url)
    if not html:
        return None

    if "gamejob" in url:
        result = site_parsers.gamejob_company_info(html)
    elif "jobkorea.co.kr" in url:
        result = site_parsers.super_company_info(html) if "jobkorea.co.kr/super/" in html[:2000] else site_parsers.jobkorea_company_info(html)
    else:
        return await _llm_fallback_extract(html_to_text(html))

    return result if _has_any_value(result) else await _llm_fallback_extract(html_to_text(html))


async def collect_recruit_site_profile(
    job_posting_url: str,
    company_info_url: str | None = None,
    company_info_screenshot_paths: list[str | Path] | None = None,
) -> dict | None:
    if company_info_url:
        result = await _fetch_and_parse(company_info_url)
        if result:
            return result

    posting_html = await fetch_page_html(job_posting_url)
    if posting_html:
        company_url = _find_company_link(posting_html, job_posting_url)
        if company_url:
            result = await _fetch_and_parse(company_url)
            if result:
                return result
        # 기업정보 페이지로 못 옮겨가도 공고 페이지 자체 텍스트에 기업정보가 섞여 있을 수 있음
        result = await _llm_fallback_extract(html_to_text(posting_html))
        if result:
            return result

    if company_info_screenshot_paths:
        images = [Path(p).read_bytes() for p in company_info_screenshot_paths]
        raw = await llm_client.vision_extract(
            images,
            "이 스크린샷들은 기업정보 화면입니다. 화면에 보이는 기업정보를 아래 JSON 형식으로 정리해주세요"
            "(정보 없는 필드는 null, benefits는 없으면 빈 배열). 다른 설명 없이 순수 JSON만 출력하세요:\n"
            + _JSON_SCHEMA_TEXT,
        )
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw)
        try:
            info = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return info if _has_any_value(info) else None

    return None
