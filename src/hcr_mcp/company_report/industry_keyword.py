"""산업/사업분야 키워드 자동 도출 — competitor_finder.py의 "한국 {키워드} 기업" 검색(키워드마다
반복, query fan-out), news/collector.py의 collect_industry_trend(업종 동향 검색)에 쓰인다.

job_posting/schemas.py에 원문 그대로("반도체사업부"/"인사팀" 등 구분 없이) 저장되므로 회사의
사업분야와 무관한 지원부서일 수 있어, 관련 있을 때만 반영하도록 프롬프트에서 별도로 지시한다.
근거가 전혀 없으면 추측하지 않고 빈 리스트(company_profile_collector.py의 환각 방지 원칙과 동일)."""

from typing import Any

from pydantic import BaseModel, Field

from hcr_mcp import llm_client

_MAX_KEYWORDS = 5  # query fan-out 권장 상한(2~5개) 중 상단 채택 — 검색 비용도 늘어나는 지점이라 무제한은 안 함

_SYSTEM_PROMPT = """당신은 채용공고와 회사 정보를 바탕으로, 뉴스·경쟁사 검색에 쓸 산업/사업분야
키워드을 뽑는 전문가입니다.

규칙:
- 입력에 언급된 회사의 사업/서비스 영역을 전부 찾아, 각각 짧은 명사구 하나로 표현하세요(예:
  "반도체", "특허정보서비스", "특허조사", "이차전지 소재"). 같은 사업 영역이 여러 곳에서
  중복 언급되면(예: 홈페이지와 채용사이트 둘 다) 더 명확하고 구체적인 표현 하나로 합치세요.
- 회사 사업에서 차지하는 비중이 큰 순서로 정렬하세요. 최대 5개까지만 반환하고, 그보다 많으면
  가장 해당 기업에 주요한 5개만 고르세요.
- 부서명(팀/본부/사업부)이 "인사팀"/"경영지원팀"처럼 회사의 사업분야와 무관한 지원부서면
  반영하지 마세요. "반도체사업부"처럼 구체적인 사업 영역을 가리키면 하나의 키워드로 포함하세요.
- 뉴스·경쟁사 검색에 쓸 키워드라는 것을 유념하세요. 좋은 검색 결과가 나와야 합니다.
- 입력에 근거가 전혀 없으면 빈 리스트를 반환하세요. 추측해서 지어내지 마세요."""

_HUMAN_PROMPT = """[회사명]
{company_name}

[공고 부서/팀명] (없으면 "정보 없음")
{department}

[직무명] (없으면 "정보 없음")
{job_title}

[회사 사업 설명] (없으면 "정보 없음")
{business_description}

[채용사이트 기업정보: 주요사업/업종] (없으면 "정보 없음")
{recruit_site_industry}"""


class _IndustryKeywords(BaseModel):
    industry_keywords: list[str] = Field(description="검색 쿼리에 쓸 산업·사업분야 키워드들(각 한글은 2~20자 이내, 영어는 2~100자 이내, 중요도순, 최대 5개). 판단할 근거가 전혀 없으면 빈 리스트")


def _company_profile_business_description(company_profile: dict[str, Any] | None) -> str | None:
    if not company_profile or not company_profile.get("crawl_success"):
        return None
    parts = [company_profile.get("business_description"), *(company_profile.get("main_products_services") or [])]
    return " / ".join(p for p in parts if p) or None


def _recruit_site_industry(recruit_site: dict[str, Any] | None) -> str | None:
    basic_info = (recruit_site or {}).get("basic_info") or {}
    return basic_info.get("주요사업") or basic_info.get("산업") or None


async def derive_industry_keywords(
    company_name: str,
    department: str | None = None,
    job_title: str | None = None,
    company_profile: dict[str, Any] | None = None,
    recruit_site: dict[str, Any] | None = None,
) -> list[str]:
    business_description = _company_profile_business_description(company_profile)
    recruit_site_industry = _recruit_site_industry(recruit_site)

    if not any((department, business_description, recruit_site_industry)):
        return []  # 근거 없이 LLM에게 물어봐야 결국 빈 리스트이거나 추측 — 호출 자체를 생략

    chain = llm_client.structured_chain(_SYSTEM_PROMPT, _HUMAN_PROMPT, _IndustryKeywords)
    result: _IndustryKeywords = await llm_client.safe_ainvoke(
        chain,
        {
            "company_name": company_name,
            "department": department or "정보 없음",
            "job_title": job_title or "정보 없음",
            "business_description": business_description or "정보 없음",
            "recruit_site_industry": recruit_site_industry or "정보 없음",
        },
    )
    return result.industry_keywords[:_MAX_KEYWORDS]
