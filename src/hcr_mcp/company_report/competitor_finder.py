"""경쟁사 이슈 수집 — 실제 검색 결과에 등장한 회사명만 후보로 뽑고, 후보별로 다시 자료를
모아 기업 뉴스와 같은 형태(gist+event_id+중요도)로 토픽 정리한다. LLM에게 "경쟁사가 누구냐"고
그냥 물어보면 모델 자체 지식(오래됐거나 틀릴 수 있음)에 의존하게 돼 이 프로젝트의 원칙
("주어지지 않은 사실을 지어내지 않는다", prompts.py 참고)에 어긋난다 — 그래서 검색으로 실제
등장한 회사명만 후보로 뽑는다.

경쟁사 후보 발견은 두 검색을 병행한다:
1. 좁은 질의("기업 {company_name} 경쟁사"): company_name을 직접 언급하며 경쟁 관계를 설명하는
   기사 위주. 네이버/web_search 둘 다 같은 쿼리 문자열을 쓴다(예전엔 web_search 쪽에만
   industry_keyword를 덧붙여 두 채널이 서로 다른 걸 검색했음, 2026-07-22 수정).
2. 넓은 질의("한국 {사업 영역} 기업"): 업종 전체를 나열하는 기사(예: "국내 특허 검색 서비스
   기업 Top 5") — 1번만으로는 "경쟁사"라는 단어가 직접 등장하는 기사가 드문 문제를 보완한다.
   회사가 사업을 여러 개 겸영하면(industry_keyword.derive_industry_keywords가 최대 5개까지
   뽑음) 사업 영역마다 따로 검색한다(query fan-out). 앞에 "한국"을 붙이는 이유: LLM 내장
   web_search가 기본적으로 미국/글로벌 기준 결과를 우선할 수 있어(실측: "한국" 없이 검색하면
   국내와 무관한 결과가 섞여 나옴) 국내 기준으로 좁힌다.
발견된 후보는 news_collector.collect_candidate_topics로 넘겨 후보별 병렬 심화 수집(네이버
검색+LLM web_search) + 토픽 정리를 맡긴다.

후보 발견(위 두 검색)은 "이름이 실제로 등장하는지"만 보고 뽑기 때문에, 사업 영역별 넓은
질의(query fan-out)로 검색을 늘릴수록 이름만 우연히 겹치는 무관한 회사(동명이인, 훨씬 큰
일반 사업을 하는 대기업 등)도 같이 늘어난다(실측 확인: ㈜윕스에서 "AI 개발" 같은 넓은 키워드로
삼성전자/LG/카카오/네이버 등이 섞여 들어옴). 그래서 후보별 심화 수집(collect_candidate_topics)이
모아온 실제 기사 내용을 근거로 "이 후보가 진짜 경쟁사인지"를 한 번 더 검증하고, 통과 못 한
후보는 최종 토픽에서 제외한다(_verify_competitor).

네이버 뉴스검색은 발견 단계(narrow+broad 쿼리)든 후보별 심화 수집이든 전부 순차(직렬)로만
호출한다(2026-07-22 수정) — 동시에 여러 쿼리를 네이버에 보내면 봇으로 감지돼 요청의 절반
이상이 403으로 차단됨을 실측 확인(46개 동시 요청 중 28개 차단). news_collector._naver_search_page1이 이 직렬 호출 + 전송 실패 시 무한 재시도를
담당한다(news/collector.py의 _fetch_search_page와 동일 패턴). web_search(LLM)는 이 문제가
없어 그대로 병렬 실행한다."""

import asyncio
import functools
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from hcr_mcp import llm_client
from hcr_mcp.company_report.company_profile_collector import _normalize_name, strip_entity_prefix
from hcr_mcp.company_report.news import collector as news_collector


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


async def _search_and_extract(naver_text: str, web_query: str, extraction_system_prompt: str) -> list[str]:
    """이미 가져온 네이버 검색 텍스트(naver_text) + LLM web_search 결과를 합쳐 이름 목록만
    구조화 추출한다. 네이버 호출은 호출자(_discover_candidates)가 미리 직렬로 해둔 것을 받는다
    — 네이버는 동시에 여러 쿼리를 보내면 봇 차단(403)이 걸림(실측 확인, news_collector.
    _naver_search_page1 참고), 반면 web_search는 이 문제가 없어 각 후보 발견 질의마다 병렬로
    호출해도 된다. web_search 단독으로는 sources가 비어(실측: "sources": null) 근거 검증이 안
    되는 경우가 많은데, 네이버 텍스트를 함께 주면 실제 근거가 있는 후보가 늘어난다. 추출 실패도
    예외 없이 빈 리스트로(경쟁사 후보는 보조 정보)."""
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


async def _discover_candidates(company_name: str, industry_keywords: list[str]) -> list[str]:
    """경쟁사 후보 회사명을 좁은 질의("기업 X 경쟁사", company_name 직접 언급) + 넓은 질의
    ("한국 {사업 영역} 기업", 사업 영역마다 반복)로 모아 자기 자신을 제외하고 중복 제거해
    반환한다. company_name은 검색 쿼리 구성 전 법인 표기를 제거한다(strip_entity_prefix — 모든
    검색 쿼리 구성 지점에서 공통 적용).

    네이버 검색은 쿼리(좁은 질의 1개 + 넓은 질의 최대 5개)마다 순차로(직렬) 호출한다 —
    news_collector._naver_search_page1 참고, 동시에 여러 쿼리를 보내면 네이버가 봇으로 감지해
    403을 반환함을 실측 확인. web_search+추출(_search_and_extract)은 네이버와 무관하니 그대로
    병렬 실행한다."""
    company_name = strip_entity_prefix(company_name)
    narrow_query = f'기업 "{company_name}" 경쟁사'
    broad_queries = [f"한국 {keyword} 기업" for keyword in industry_keywords]
    queries = [narrow_query, *broad_queries]

    naver_texts: list[str] = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for query in queries:
            results = await news_collector._naver_search_page1(client, query)
            naver_texts.append("\n".join(f"- {a['title']}: {a.get('snippet') or ''}" for a in results))

    narrow_task = _search_and_extract(naver_texts[0], narrow_query, _SYSTEM_PROMPT)
    broad_tasks = [
        _search_and_extract(naver_texts[i + 1], broad_queries[i], _LIST_EXTRACTION_PROMPT)
        for i in range(len(broad_queries))
    ]
    narrow_candidates, *broad_candidate_lists = await asyncio.gather(narrow_task, *broad_tasks)

    own = _normalize_name(company_name)
    seen: dict[str, str] = {}
    for name in narrow_candidates + [n for candidates in broad_candidate_lists for n in candidates]:
        norm = _normalize_name(name)
        if norm and norm != own and norm not in seen:
            seen[norm] = name
    return list(seen.values())


class _CandidateClassification(BaseModel):
    category: Literal["competitor", "related_institution", "unrelated"] = Field(
        description=(
            "아래 기사들만 근거로 판단: 대상 회사와 같은 고객층을 놓고 실제로 시장에서 경쟁하는 "
            "관계(공공기관이어도 상관없음 — 예: 우체국과 대한통운, 코레일과 동양고속, 국민연금과 "
            "증권사도 각각 실제 경쟁 관계)면 competitor. 대상 회사가 사업 기반으로 활용하는 "
            "데이터 제공처·인프라·협회 등 산업 생태계 관련이지만 같은 고객을 두고 경쟁하지는 "
            "않으면 related_institution. 둘 다 아니면 unrelated"
        )
    )


_VERIFY_SYSTEM_PROMPT = """당신은 뉴스 기사 내용만 보고 두 회사·기관의 관계를 판단하는 벤치마킹
전문가입니다.

규칙:
- 판단 기준은 "공공기관인지 민간기업인지"가 아니라 **대상 회사와 같은 고객층을 놓고 실제로
  시장에서 경쟁하는 관계인지**입니다. 공공기관·공기업도 얼마든지 경쟁사일 수 있습니다(예:
  우체국은 대한통운의 경쟁사, 코레일은 동양고속의 경쟁사, 국민연금도 자산운용 시장에서
  증권사들의 경쟁사) — 이런 경우 competitor로 분류하세요.
- 후보가 대상 회사의 사업 기반이 되는 데이터 제공처·인프라·협회 등이라 산업 생태계와는
  관련 있지만, 같은 고객을 놓고 직접 경쟁하지는 않으면(예: 대상 회사가 그 기관의 공개
  데이터를 가공해 상업 서비스를 만드는 관계) related_institution으로 분류하세요.
- 기사가 후보와 이름만 같고 실제로는 전혀 다른 업종/회사에 관한 내용이면(동명이인, 무관한
  산업) unrelated로 분류하세요.
- 후보가 여러 사업을 겸영하는 대기업/포털이고 기사에 대상 회사와 겹치는 구체적 사업 내용이
  안 나오면(예: 넓은 업종 키워드 검색에 우연히 걸린 경우) unrelated로 분류하세요.
- 기사에 후보의 사업 내용이 전혀 안 나오거나 판단할 근거가 부족하면 unrelated로 분류하세요 —
  확신 없으면 competitor/related_institution 어느 쪽으로도 포함하지 않습니다."""

_VERIFY_HUMAN_PROMPT = """[대상 회사] {company_name}
[대상 회사 사업 영역] {industry_keywords}
[검증 대상 후보] {candidate}

[후보에 대해 수집된 기사]
{articles_text}"""


async def _classify_candidate(
    candidate: str, articles: list[dict], company_name: str, industry_keywords: list[str]
) -> str:
    """collect_candidate_topics가 이미 모아온 후보별 실제 기사로 "competitor"/"related_institution"/
    "unrelated" 중 하나로 분류한다. 발견 단계(넓은 질의)의 스니펫만으로는 대상 회사가 뭔지조차
    모르는 채로 이름만 뽑기 때문에(_LIST_EXTRACTION_PROMPT는 company_name을 안 받음) 그
    단계에서는 정확한 판단이 어렵고, 후보별 심화 수집이 이미(분류 여부와 무관하게) 실행되는
    단계라 여기에 짧은 분류 호출 1번만 얹는 게 추가 비용도 적다."""
    articles_text = "\n\n".join(f"[{a.get('title', '')}] {a.get('body') or a.get('snippet') or ''}" for a in articles[:5])
    if not articles_text.strip():
        return "unrelated"
    chain = llm_client.structured_chain(_VERIFY_SYSTEM_PROMPT, _VERIFY_HUMAN_PROMPT, _CandidateClassification)
    try:
        result: _CandidateClassification = await llm_client.safe_ainvoke(
            chain,
            {
                "company_name": company_name,
                "industry_keywords": ", ".join(industry_keywords) or "정보 없음",
                "candidate": candidate,
                "articles_text": articles_text[:6000],
            },
        )
        return result.category
    except Exception:  # noqa: BLE001 — 분류 실패는 보수적으로 제외(정보 없음이 부정확한 정보보다 낫다는 원칙)
        return "unrelated"


async def collect_competitor_issues(
    company_name: str, industry_keywords: list[str], on_raw_ready
) -> tuple[list[dict], list[dict]]:
    """경쟁사 후보를 발견한 뒤, 후보별로 다시 자료를 모아 기업 뉴스와 같은 형태(gist+event_id+
    중요도)로 토픽 정리해 반환한다(news_collector.collect_candidate_topics 참고). 후보별로 모인
    실제 기사를 근거로 competitor/related_institution/unrelated로 분류해(_classify_candidate)
    unrelated는 제외한다 — related_institution(공공기관·데이터 제공처 등, 경쟁 관계는 아니지만
    산업 동향 파악에 도움되는 대상)은 버리지 않고 별도로 반환해 호출자(report_builder.py)가
    industry_topics에 합류시킨다.
    on_raw_ready: 후보별 자료 수집 직후 즉시 호출(의무 저장 — 분류 결과와 무관하게 수집된
    원문은 전부 저장, 다른 collect_* 함수와 동일 패턴).
    반환: (경쟁사 토픽, 관련 기관 토픽)."""
    candidates = await _discover_candidates(company_name, industry_keywords)
    classify = functools.partial(_classify_candidate, company_name=company_name, industry_keywords=industry_keywords)
    return await news_collector.collect_candidate_topics(candidates, on_raw_ready, classify=classify)
