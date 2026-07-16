"""경쟁사 후보 검색 — 실제 검색 결과(제목+스니펫)에 등장한 회사명만 추출한다. LLM에게
"경쟁사가 누구냐"고 그냥 물어보면 모델 자체 지식(오래됐거나 틀릴 수 있음)에 의존하게 돼
이 프로젝트의 원칙("주어지지 않은 사실을 지어내지 않는다", prompts.py 참고)에 어긋난다 —
그래서 검색으로 실제 등장한 회사명만 뽑게 한다.

소스: (1) 네이버 뉴스검색 1페이지(news/collector.py의 검색+파싱 로직 재사용 — 페이지네이션·
기간 필터 없는 가벼운 단발 조회), (2) llm_client.web_search(OpenAI 호스팅 웹검색, 서버
사이드라 이쪽에서 봇 차단·JS 렌더링 문제를 겪지 않음)."""

import httpx
from pydantic import BaseModel, Field

from hcr_mcp import llm_client
from hcr_mcp.company_report.company_profile_collector import _normalize_name
from hcr_mcp.company_report.news.collector import _HEADERS, _SEARCH_URL, _parse_search_page

_REQUEST_TIMEOUT = 10


async def _naver_page1(query: str) -> list[dict]:
    """네이버 뉴스검색 1페이지만(페이지네이션·기간 필터 없음) — 가벼운 단발 조회용.
    ssc=tab.news.all/sm=tab_opt는 news/collector.py의 _date_range_params와 동일하게
    '뉴스' 탭 결과를 받기 위한 필수 파라미터(이게 없으면 _parse_search_page가 기대하는
    HTML 구조와 다른 통합검색 페이지가 반환된다) — 날짜 구간 필터(ds/de/nso 등)만 뺐다."""
    params = {"query": query, "ssc": "tab.news.all", "sm": "tab_opt", "start": "1"}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            resp = await client.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError:
            return []  # 검색 실패는 조용히 빈 결과로 — 경쟁사 후보는 보조 정보일 뿐 필수 경로가 아님
    return _parse_search_page(resp.text)


class _CompetitorNames(BaseModel):
    competitors: list[str] = Field(description="검색 결과 텍스트에 실제로 등장한 경쟁사 회사명만(추측 금지)")


_SYSTEM_PROMPT = """당신은 검색 결과 텍스트에서 특정 회사의 경쟁사를 선별하는 산업 및 사업 전문가입니다.

규칙:
- 아래 주어진 검색 결과(제목·스니펫)에 실제로 등장하는 회사명만 뽑으세요.
- 텍스트에 없는 회사명을 알고 있는 지식으로 추가하지 마세요 — 이 목록에 없는 근거는 지어내지 않습니다.
- 대상 회사 자신은 목록에서 제외하세요.
- 같은 회사가 여러 표기로 나오면(예: "네이버" / "NAVER") 하나로 합치세요.
- 경쟁사 파악에 불필요한 것(산업 용어, 인물명 등)은 포함하지 마세요."""


async def find_competitors(company_name: str, industry_keyword: str | None = None) -> list[str]:
    """회사의 경쟁사 후보를 실제 검색 결과에 근거해서만 찾는다(LLM 자체 지식으로 나열하게
    하지 않음 — 모듈 docstring 참고). 네이버 뉴스검색 1페이지 + OpenAI 호스팅 웹검색
    (llm_client.web_search) 결과를 모아 그 텍스트 안에서만 회사명을 추출한다. 두 소스 모두
    실패해도(검색 결과 없음, 웹검색 오류 등) 예외를 던지지 않고 빈 리스트를 반환한다."""
    query = f'기업 "{company_name}" 경쟁사'
    naver_results = await _naver_page1(query)
    naver_text = "\n".join(f"- {a['title']}: {a.get('snippet') or ''}" for a in naver_results)

    try:
        web_query = f"{company_name} 경쟁사" + (f" {industry_keyword}" if industry_keyword else "")
        web_text = await llm_client.web_search(web_query)
    except Exception:  # noqa: BLE001 — 웹검색 실패해도 네이버 결과만으로 계속 진행(보조 정보 경로)
        web_text = ""

    combined = "\n\n".join(t for t in (naver_text, web_text) if t)
    if not combined:
        return []

    chain = llm_client.structured_chain(_SYSTEM_PROMPT, "회사명: {company_name}\n\n검색 결과:\n{combined}", _CompetitorNames)
    try:
        result: _CompetitorNames = await llm_client.safe_ainvoke(chain, {"company_name": company_name, "combined": combined})
    except Exception:  # noqa: BLE001 — 추출 실패해도 예외를 던지지 않음(경쟁사 목록은 보조 정보)
        return []

    own = _normalize_name(company_name)
    return [c for c in result.competitors if _normalize_name(c) != own]
