"""회사 공식 홈페이지 크롤링 + LLM 정보 추출 (HcR/company-crawler/main.py copy-adapt).

원본 차이점: 모듈 임포트 시점에 OPENAI_API_KEY를 읽어 없으면 즉시 크래시하던 부분을 제거하고
hcr_mcp.llm_client(BYOK)를 사용. requests → httpx(비동기)로 교체, lxml 대신 stdlib
html.parser 사용(새 의존성 추가 안 함). 검색·크롤링 실패는 원본처럼 부분 실패로 조용히
넘어간다(임의의 회사 홈페이지가 하나 접속 안 되는 것은 정상적인 현상이지, 잡아서 사용자에게
알릴 만한 오류가 아니다) — 반면 LLM 호출 실패는 llm_client.chat()이 이미 명확한 메시지로 변환한다.
"""

import json
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from hcr_mcp import llm_client, net

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

_MAX_TEXT_LENGTH = 4500
_REQUEST_TIMEOUT = 10

# 공식 홈페이지가 아닌데 검색 상위에 자주 잡히는 도메인(채용·기업정보·SNS·백과·공공 포털)
_BLOCKED_DOMAINS = (
    "google.com", "search.naver.com", "blog.naver.com", "cafe.naver.com",
    "post.naver.com", "in.naver.com", "linkedin.com", "facebook.com",
    "instagram.com", "youtube.com", "twitter.com", "x.com", "tistory.com",
    "namu.wiki", "wikipedia.org",
    "jobkorea.co.kr", "saramin.co.kr", "wanted.co.kr", "rocketpunch.com",
    "jobplanet.co.kr", "incruit.com", "gamejob.co.kr", "albamon.com",
    "catch.co.kr", "linkareer.com", "worknet.go.kr", "jasoseol.com",
    "nicebizinfo.com", "nicednb.com", "creditbank.co.kr", "nts.go.kr",
    "alio.go.kr", "data.go.kr", "ftc.go.kr", "kotra.or.kr", "innobiz.or.kr",
    "kssn.net", "bizno.net", "kepco.co.kr",
)

_STRIP_TAGS = ["script", "style", "noscript", "header", "footer", "nav"]


def _is_blocked_url(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return any(domain in netloc for domain in _BLOCKED_DOMAINS)


async def _fallback_naver_search(client: httpx.AsyncClient, company_name: str) -> str | None:
    try:
        resp = await client.get(
            "https://search.naver.com/search.naver",
            params={"query": f"{company_name} 공식 홈페이지"},
            headers=_HEADERS,
            timeout=_REQUEST_TIMEOUT,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not href.startswith("http") or _is_blocked_url(href):
                continue
            netloc = urlparse(href).netloc.lower()
            if "naver.com" in netloc or "google" in netloc:
                continue
            return href
    except httpx.HTTPError as e:
        net.raise_if_ssl_trust_error(e)
    return None


async def search_company_url(client: httpx.AsyncClient, company_name: str) -> str | None:
    return await _fallback_naver_search(client, company_name)


def _extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        return _extract_text(BeautifulSoup(resp.content, "html.parser"))
    except httpx.HTTPError as e:
        net.raise_if_ssl_trust_error(e)
        return ""


def _find_link_by_keywords(soup: BeautifulSoup, base_url: str, keywords: list[str]) -> str | None:
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if any(kw in href.lower() or kw in text for kw in keywords):
            if href.startswith("http"):
                return href
            if href.startswith("/"):
                parsed = urlparse(base_url)
                return f"{parsed.scheme}://{parsed.netloc}{href}"
    return None


async def crawl_page(client: httpx.AsyncClient, url: str) -> tuple[str, str, str]:
    """(본문 텍스트, 최종 URL, 실패사유) 반환. 성공 시 실패사유는 빈 문자열."""
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        sub_url = _find_link_by_keywords(soup, url, ["about", "회사소개", "company", "채용", "인재", "culture"])
        ceo_url = _find_link_by_keywords(soup, url, ["ceo", "message", "대표이사", "인삿말"])

        parts = [_extract_text(soup)]
        if sub_url:
            parts.append(await _fetch_text(client, sub_url))
        if ceo_url and ceo_url != sub_url:
            parts.append(await _fetch_text(client, ceo_url))

        text = " ".join(p for p in parts if p).strip()
        final_text = text[:_MAX_TEXT_LENGTH]
        reason = "" if final_text else "빈 페이지(본문 텍스트 없음, JS 렌더링 가능성)"
        return final_text, url, reason
    except httpx.HTTPError as e:
        net.raise_if_ssl_trust_error(e)
        return "", url, f"접속 실패({type(e).__name__})"


_SYSTEM_PROMPT = """당신은 기업 정보를 분석하는 전문가입니다.
주어진 텍스트에서 다음 정보를 추출하여 반드시 JSON 형식으로만 응답하세요.
다른 설명이나 마크다운 없이 순수 JSON만 출력하세요.
정보가 없으면 null로 표시하세요.

중요: 웹페이지 텍스트가 요청한 회사가 아니라 채용 포털(잡코리아·게임잡 등),
기업정보/신용조회 사이트(나이스평가정보 등), 정부기관, 또는 전혀 다른 회사에 관한
내용이라면 is_company_match 를 false 로 설정하세요. 텍스트가 명확히 해당 회사
자신의 소개일 때만 true 로 설정합니다."""

_USER_PROMPT_TEMPLATE = """회사명: {company_name}
웹페이지 텍스트:
{webpage_text}

위 내용을 분석하여 아래 JSON 형식으로 추출해줘:
{{
  "company_name": "회사명",
  "website_url": "공식 홈페이지 URL",
  "business_description": "주요 사업 내용 (2~3문장)",
  "main_products_services": ["주요 제품/서비스 1", "주요 서비스 2"],
  "talent_values": "인재상 (없으면 null)",
  "ceo_message": "CEO 인삿말 요약 (없으면 null)",
  "is_company_match": true,
  "crawl_success": true
}}"""


def _normalize_nulls(result: dict) -> dict:
    def clean(value):
        if isinstance(value, str) and value.strip().lower() in ("null", "none", ""):
            return None
        if isinstance(value, list):
            return [v for v in value if clean(v) is not None]
        return value

    return {key: clean(value) for key, value in result.items()}


async def extract_info_with_llm(company_name: str, webpage_text: str, website_url: str) -> dict:
    user_prompt = _USER_PROMPT_TEMPLATE.format(company_name=company_name, webpage_text=webpage_text)
    raw = await llm_client.chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    result = _normalize_nulls(json.loads(raw))
    result["website_url"] = result.get("website_url") or website_url
    return result


_ENTITY_ONLY_RE = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(사\)|사단법인|\(재\)|재단법인")
_ENTITY_PREFIX_RE = re.compile(_ENTITY_ONLY_RE.pattern + r"|\s+")


def strip_entity_prefix(name: str) -> str:
    """검색 쿼리 등에 쓰기 위해 법인 형태 표기(㈜/주식회사/(유)/유한회사/(사)/사단법인/(재)/
    재단법인)만 제거한다 — 공백·대소문자는 그대로 유지(순수 표기 정리용, 검색어 가독성을
    해치지 않음). 전체 정규화(공백 제거+소문자화, 동일 회사 판별용)가 필요하면 _normalize_name.
    회사명을 그대로 검색 쿼리에 넣으면 법인 표기 차이(㈜윕스 vs 윕스)로 검색 결과가 갈릴 수
    있어(실측은 네이버에서는 차이 없었지만, 다른 검색 소스·LLM web_search에서는 보장 안 됨)
    모든 검색 쿼리 구성 지점에서 이 함수로 정리한 이름을 쓴다."""
    return re.sub(r"\s+", " ", _ENTITY_ONLY_RE.sub("", name or "")).strip()


def _normalize_name(s: str) -> str:
    return _ENTITY_PREFIX_RE.sub("", s or "").lower()


def _name_appears_in_text(company_name: str, text: str) -> bool:
    normalized_text = _normalize_name(text)
    normalized_name = _normalize_name(company_name)
    if normalized_name and normalized_name in normalized_text:
        return True
    prefix_match = re.match(r"[가-힣]{2,}", company_name)
    prefix = prefix_match.group(0)[:2] if prefix_match else ""
    return bool(prefix) and prefix in normalized_text


def _failure(company_name: str, website_url: str | None, error: str) -> dict:
    return {
        "company_name": company_name,
        "website_url": website_url,
        "business_description": None,
        "main_products_services": [],
        "talent_values": None,
        "ceo_message": None,
        "crawl_success": False,
        "error": error,
    }


async def collect_company_profile(company_name: str) -> dict:
    """회사명 → 공식 홈페이지 크롤링 + LLM 추출 결과.

    `crawl_success: False`는 오류가 아니라 정상적인 실패 결과다 — 홈페이지를 못 찾거나,
    JS 렌더링 사이트라 본문이 비거나, 검색된 페이지가 실제로는 다른 회사/포털인 경우 등
    임의의 회사 크롤링에서 흔히 발생하는 일이라 호출자는 `error` 필드를 보고 해당 섹션을
    빈 값으로 두면 된다(예외를 던지지 않음).
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        url = await search_company_url(client, company_name)
        if url:
            parsed = urlparse(url)
            url = f"{parsed.scheme}://{parsed.netloc}"
        if not url:
            return _failure(company_name, None, "URL 탐색 실패: 검색 결과에서 공식 사이트를 찾지 못함")

        text, final_url, fail_reason = await crawl_page(client, url)
        if not text:
            return _failure(company_name, final_url, fail_reason or "페이지 크롤링 실패")

    try:
        result = await extract_info_with_llm(company_name, text, final_url)
    except json.JSONDecodeError as e:
        return _failure(company_name, final_url, f"JSON 파싱 실패: {e}")

    if result.get("is_company_match") is False:
        return _failure(company_name, final_url, "이름 매칭 실패: 페이지가 요청 회사가 아닌 포털/타사 내용")
    if not _name_appears_in_text(company_name, text):
        return _failure(company_name, final_url, "이름 미확인: 본문에 회사명이 없어 동일 회사인지 확인 불가")

    description = (result.get("business_description") or "").strip()
    products = result.get("main_products_services") or []
    if not description and not products:
        return _failure(company_name, final_url, "내용 없음: 사이트는 맞으나 사업 정보를 추출하지 못함(빈/JS 페이지 가능성)")

    result.pop("is_company_match", None)
    result["crawl_success"] = True
    return result
