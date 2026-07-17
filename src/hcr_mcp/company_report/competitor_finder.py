"""경쟁사 이슈 수집 — 실제 검색 결과에 등장한 회사명만 후보로 뽑고, 후보별로 다시 자료를
모아 기업 뉴스와 같은 형태(gist+event_id+중요도)로 토픽 정리한다. LLM에게 "경쟁사가 누구냐"고
그냥 물어보면 모델 자체 지식(오래됐거나 틀릴 수 있음)에 의존하게 돼 이 프로젝트의 원칙
("주어지지 않은 사실을 지어내지 않는다", prompts.py 참고)에 어긋난다 — 그래서 검색으로 실제
등장한 회사명만 후보로 뽑는다.

경쟁사 후보 발견은 두 검색을 병행한다:
1. 좁은 질의("기업 {company_name} 경쟁사"): company_name을 직접 언급하며 경쟁 관계를 설명하는 기사 위주.
2. 넓은 질의("한국 {주력서비스/산업} 기업"): 업종 전체를 나열하는 기사(예: "국내 특허
   검색 서비스 기업 Top 5") — 1번만으로는 "경쟁사"라는 단어가 직접 등장하는 기사가 드문 문제를
   보완한다. 앞에 "한국"을 붙이는 이유: LLM 내장 web_search가 기본적으로 미국/글로벌 기준
   결과를 우선할 수 있어(실측: "한국" 없이 검색하면 국내와 무관한 결과가 섞여 나옴) 국내
   기준으로 좁힌다.
발견된 후보는 news_collector.collect_candidate_topics로 넘겨 후보별 병렬 심화 수집(네이버
검색+LLM web_search) + 토픽 정리를 맡긴다."""

from pydantic import BaseModel, Field
import asyncio
import httpx
from hcr_mcp import llm_client
from hcr_mcp.company_report.company_profile_collector import _normalize_name, strip_entity_prefix
from hcr_mcp.company_report.news import collector as news_collector
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


_SYSTEM_PROMPT = """당신은 검색 결과 텍스트에서 특정 회사의 경쟁사를 선별하는 벤치마킹 및 사업 전략 전문가입니다.

규칙:
- 아래 주어진 검색 결과(제목·스니펫)에 실제로 등장하는 회사명만 뽑으세요.
- 텍스트에 없는 회사명을 알고 있는 지식으로 추가하지 마세요 — 이 목록에 없는 근거는 지어내지 않습니다.
- 대상 회사 자신은 목록에서 제외하세요.
- 같은 회사가 여러 표기로 나오면(예: "네이버" / "NAVER") 하나로 합치세요.
- 경쟁사 파악에 불필요한 것(산업 용어, 인물명 등)은 포함하지 마세요."""

_LIST_EXTRACTION_PROMPT = """당신은 검색 결과 텍스트에서 특정 업종/서비스 분야에 속한 기업 및 서비스를
선별하는 벤치마킹 및 사업 전략 전문가입니다.

규칙:
- 아래 검색 결과(리스트/랭킹/비교 기사 등)에 실제로 등장하는 기업명이나 서비스만 뽑으세요.
- 텍스트에 없는 기업명이나 서비스를 알고 있는 지식으로 추가하지 마세요.
- 경쟁사와 관련 없는 것(업종 용어, 인물명 등)은 포함하지 마세요."""


async def _search_and_extract(naver_query: str, web_query: str, extraction_system_prompt: str) -> list[str]:
    """네이버 검색(실제 title/snippet 근거) + LLM web_search를 병행해 합친 뒤 이름 목록만
    구조화 추출한다. web_search 단독으로는 sources가 비어(실측: "sources": null) 근거 검증이
    안 되는 경우가 많은데, 같은 검색어로 네이버를 함께 돌리면 실제 근거가 있는 후보가 늘어난다.
    검색·추출 어느 단계가 실패해도 예외 없이 가능한 만큼만 진행(경쟁사 후보는 보조 정보)."""
    naver_results = await _naver_page1(naver_query)
    naver_text = "\n".join(f"- {a['title']}: {a.get('snippet') or ''}" for a in naver_results)

    try:
        web_text = await llm_client.web_search(web_query)
    except Exception:  # noqa: BLE001 — 웹검색 실패해도 네이버 결과만으로 계속 진행
        web_text = ""

    combined = "\n\n".join(t for t in (naver_text, web_text) if t)
    if not combined:
        return []

    chain = llm_client.structured_chain(extraction_system_prompt, "검색 결과:\n{combined}", _CompetitorNames)
    try:
        result: _CompetitorNames = await llm_client.safe_ainvoke(chain, {"combined": combined})
        return result.competitors
    except Exception:  # noqa: BLE001 — 추출 실패해도 예외를 던지지 않음(경쟁사 후보는 보조 정보)
        return []


async def _discover_narrow_candidates(company_name: str, industry_keyword: str | None) -> list[str]:
    """기존 좁은 질의("기업 X 경쟁사") — 원본 로직 그대로(변경 없음)."""
    narrow_query = f'기업 "{company_name}" 경쟁사'
    naver_results = await _naver_page1(narrow_query)
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
        return result.competitors
    except Exception:  # noqa: BLE001 — 추출 실패해도 예외를 던지지 않음(경쟁사 목록은 보조 정보)
        return []


async def _discover_candidates(company_name: str, industry_keyword: str | None) -> list[str]:
    """경쟁사 후보 회사명을 좁은 질의(경쟁사 직접 언급, 기존 로직)+넓은 질의(업종 전체 나열,
    신규 추가 — 네이버+web_search 병행)로 동시에 모아 자기 자신을 제외하고 중복 제거해 반환한다.
    company_name은 검색 쿼리 구성 전 법인 표기를 제거한다(strip_entity_prefix — 모든 검색 쿼리
    구성 지점에서 공통 적용)."""
    company_name = strip_entity_prefix(company_name)

    narrow_task = _discover_narrow_candidates(company_name, industry_keyword)
    if industry_keyword:
        broad_query = f"한국 {industry_keyword} 기업"
        broad_task = _search_and_extract(broad_query, broad_query, _LIST_EXTRACTION_PROMPT)
    else:
        broad_task = _no_candidates()

    narrow_candidates, broad_candidates = await asyncio.gather(narrow_task, broad_task)

    own = _normalize_name(company_name)
    seen: dict[str, str] = {}
    for name in narrow_candidates + broad_candidates:
        norm = _normalize_name(name)
        if norm and norm != own and norm not in seen:
            seen[norm] = name
    return list(seen.values())


async def _no_candidates() -> list[str]:
    """industry_keyword가 없을 때 narrow_task와 같은 반환 타입을 맞추기 위한 빈 결과 —
    asyncio.gather에 조건부로 다른 코루틴을 섞어 넣을 수 있게 한다."""
    return []


async def collect_competitor_issues(
    company_name: str, industry_keyword: str | None, on_raw_ready
) -> tuple[list[dict], list[dict]]:
    """경쟁사 후보를 발견한 뒤, 후보별로 다시 자료를 모아 기업 뉴스와 같은 형태(gist+event_id+
    중요도)로 토픽 정리해 반환한다(news_collector.collect_candidate_topics 참고).
    on_raw_ready: 후보별 자료 수집 직후 즉시 호출(의무 저장 — 다른 collect_* 함수와 동일 패턴)."""
    candidates = await _discover_candidates(company_name, industry_keyword)
    return await news_collector.collect_candidate_topics(candidates, on_raw_ready)
