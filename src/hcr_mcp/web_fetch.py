"""URL → HTML/정제된 텍스트. job_posting/collector.py와 company_report의 여러 콜렉터가 공유하는
스크래핑 유틸 (사이트별 셀렉터는 이 모듈 밖 — 여기는 순수 fetch만 담당)."""

import logging

import httpx
from bs4 import BeautifulSoup

from hcr_mcp import net

logger = logging.getLogger("hcr_mcp.web_fetch")

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


async def fetch_page_html(url: str) -> tuple[str, str] | None:
    """원본 HTML과 리다이렉트를 다 따라간 뒤의 최종 URL. CSS 셀렉터 기반 파서(site_parsers.py
    등)가 쓴다. 최종 URL을 같이 반환하는 이유: URL 단축 서비스(예: joburl.kr)나 모바일
    서브도메인(m.jobkorea.co.kr)을 거치면 호출자가 넘긴 원본 url만으로는 실제 도착한 사이트를
    판별 못 한다(실측: job_posting/collector.py의 잡코리아 판별이 원본 url 기준이라 단축 URL
    입력 시 상세 본문 iframe 트릭이 아예 안 걸림)."""
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        net.raise_if_ssl_trust_error(e)
        logger.warning("URL 스크래핑 실패(%s): %s", url, e)
        return None
    return resp.text, str(resp.url)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


async def fetch_page_text(url: str) -> str | None:
    """정제된 본문 텍스트. 사이트별 셀렉터 없이 일반 텍스트만 필요할 때(LLM 추출 등) 쓴다."""
    fetched = await fetch_page_html(url)
    if not fetched:
        return None
    html, _final_url = fetched
    return html_to_text(html) or None
