"""회사 분석 보고서 조립 — base(DART+홈페이지+채용사이트+LLM 합성) + 뉴스(회사 이슈/산업 동향)를
병합해 schemas.py의 CompanyReportBase 형태로 최종 report.json을 만든다.

뉴스 원문 수집·저장(collect_and_save_news)을 base 합성보다 먼저 만든다 — 스크래핑+LLM 호출로
만들어진 raw_issues는 재수집 비용이 가장 크고 손실 위험이 큰 데이터라, 다른 조립 단계보다 먼저
로컬에 안전하게 남겨야 한다(우선순위 지시, notes/phase2_plan.md 참고). base 리포트 합성·임베딩
저장·최종 병합은 다음 단계에서 이 파일에 추가한다."""

import json

from hcr_mcp.company_report.news import collector as news_collector
from hcr_mcp.storage import Storage


async def collect_and_save_news(
    storage: Storage,
    company_name: str,
    industry_keyword: str | None,
    ceo_name: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """회사 이슈 + (있으면) 산업 동향 뉴스를 수집하고, 원문(본문 포함 raw_issues)을 각각 수집
    직후 바로 로컬에 저장한다. 반환: (회사 이슈 토픽 목록, 산업 동향 토픽 목록) — 둘 다 이후
    base 리포트 합성 프롬프트(company_report/prompts.py)의 입력으로 쓰인다.

    회사 이슈와 산업 동향을 각각 수집 직후 개별 저장하므로, 산업 동향 수집이 실패해도 이미 저장된
    회사 이슈 원문은 남는다."""
    company_topics, company_raw = await news_collector.collect_recent_issues(company_name, ceo_name)
    _save_raw_news(storage, company_name, "news_company_raw.json", company_raw)

    industry_topics: list[dict] = []
    if industry_keyword:
        industry_topics, industry_raw = await news_collector.collect_industry_trend(industry_keyword)
        _save_raw_news(storage, company_name, "news_industry_raw.json", industry_raw)

    return company_topics, industry_topics


def _save_raw_news(storage: Storage, company_name: str, filename: str, raw_issues: list[dict]) -> None:
    content = json.dumps(raw_issues, ensure_ascii=False, indent=2).encode("utf-8")
    storage.save_raw("company_report", company_name, filename, content)
