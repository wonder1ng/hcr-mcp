"""URL → HTML/정제된 텍스트. job_posting/collector.py와 company_report의 여러 콜렉터가 공유하는
스크래핑 유틸 (사이트별 셀렉터는 이 모듈 밖 — 여기는 순수 fetch만 담당)."""

import logging

import httpx
from bs4 import BeautifulSoup

from hcr_mcp import net

logger = logging.getLogger("hcr_mcp.web_fetch")

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


async def fetch_page_html(url: str) -> str | None:
    """원본 HTML 그대로. CSS 셀렉터 기반 파서(site_parsers.py 등)가 쓴다."""
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        net.raise_if_ssl_trust_error(e)
        logger.warning("URL 스크래핑 실패(%s): %s", url, e)
        return None
    return resp.text


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


async def fetch_page_text(url: str) -> str | None:
    """정제된 본문 텍스트. 사이트별 셀렉터 없이 일반 텍스트만 필요할 때(LLM 추출 등) 쓴다."""
    html = await fetch_page_html(url)
    if not html:
        return None
    return html_to_text(html) or None
