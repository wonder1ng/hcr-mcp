"""산업/사업분야 키워드 자동 도출 — competitor_finder.py의 "한국 {industry_keyword} 기업"
검색, news/collector.py의 collect_industry_trend(업종 동향 검색)에 쓰인다.

우선순위: 공고의 department(팀/본부/사업부명) > 회사 전체 프로필(홈페이지 크롤링
business_description/main_products_services, 채용사이트 기업정보 "주요사업"/"산업"). department는
job_posting/schemas.py에 원문 그대로("반도체사업부"/"인사팀" 등 구분 없이) 저장되므로 여기서
그대로 검색어로 쓰지 않는다 — "인사팀"처럼 회사 사업분야와 무관한 지원부서인 경우가 흔해서,
사용 가능한 근거를 LLM에게 넘겨 실제 검색어로 쓸 짧은 키워드 하나를 판단하게 한다. 근거가 전혀
없으면 추측하지 않고 None(company_profile_collector.py의 환각 방지 원칙과 동일)."""

from typing import Any

from pydantic import BaseModel, Field

from hcr_mcp import llm_client

_SYSTEM_PROMPT = """당신은 채용공고와 회사 정보를 바탕으로, 뉴스·경쟁사 검색에 쓸 산업/사업분야
키워드 하나를 뽑는 전문가입니다.

규칙:
- 회사가 실제로 속한 산업·사업분야를 짧은 명사구 하나로 표현하세요(예: "반도체", "특허정보서비스",
  "이차전지 소재").
- 부서명(팀/본부/사업부)이 "인사팀"/"경영지원팀"처럼 회사의 사업분야와 무관한 지원부서라면
  참고하지 말고, 회사 전체 사업 정보를 근거로 판단하세요.
- 부서명이 "반도체사업부"처럼 구체적인 사업 영역을 가리키면 전사 정보보다 이 정보를
  우선하세요(더 구체적인 검색에 유리).
- 입력에 근거가 전혀 없으면 industry_keyword를 null로 반환하세요. 추측해서 지어내지 마세요."""

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


class _IndustryKeyword(BaseModel):
    industry_keyword: str | None = Field(description="검색 쿼리에 쓸 산업·사업분야 키워드 하나(2~10자 내외), 판단할 근거가 전혀 없으면 null")


def _company_profile_business_description(company_profile: dict[str, Any] | None) -> str | None:
    if not company_profile or not company_profile.get("crawl_success"):
        return None
    parts = [company_profile.get("business_description"), *(company_profile.get("main_products_services") or [])]
    return " / ".join(p for p in parts if p) or None


def _recruit_site_industry(recruit_site: dict[str, Any] | None) -> str | None:
    basic_info = (recruit_site or {}).get("basic_info") or {}
    return basic_info.get("주요사업") or basic_info.get("산업") or None


async def derive_industry_keyword(
    company_name: str,
    department: str | None = None,
    job_title: str | None = None,
    company_profile: dict[str, Any] | None = None,
    recruit_site: dict[str, Any] | None = None,
) -> str | None:
    business_description = _company_profile_business_description(company_profile)
    recruit_site_industry = _recruit_site_industry(recruit_site)

    if not any((department, business_description, recruit_site_industry)):
        return None  # 근거 없이 LLM에게 물어봐야 결국 null이거나 추측 — 호출 자체를 생략

    chain = llm_client.structured_chain(_SYSTEM_PROMPT, _HUMAN_PROMPT, _IndustryKeyword)
    result: _IndustryKeyword = await llm_client.safe_ainvoke(
        chain,
        {
            "company_name": company_name,
            "department": department or "정보 없음",
            "job_title": job_title or "정보 없음",
            "business_description": business_description or "정보 없음",
            "recruit_site_industry": recruit_site_industry or "정보 없음",
        },
    )
    return result.industry_keyword
