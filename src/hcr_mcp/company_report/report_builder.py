"""회사 분석 보고서 조립 — base(DART+홈페이지+채용사이트+LLM 합성) + 뉴스(회사 이슈/산업 동향)를
병합해 schemas.py의 CompanyReportBase 형태로 최종 report.json을 만든다.

뉴스 원문 수집·저장(collect_and_save_news)을 base 합성보다 먼저 만든다 — 스크래핑으로 확보한
기사 원문은 재수집 비용이 가장 크고 손실 위험이 큰 데이터라, 다른 조립 단계보다 먼저 로컬에
안전하게 남겨야 한다(우선순위 지시, notes/phase2_plan.md 참고). 그래서 news_collector의
그룹핑/임베딩/요약(선별 단계, 실패 가능성 있음) 결과를 기다렸다가 저장하지 않고, 매 검색
라운드 스크래핑 직후(on_raw_ready 콜백)마다 즉시 저장한다 — 선별 단계가 도중에 실패해도
이미 스크래핑된 원문은 남는다. base 리포트 합성·임베딩 저장·최종 병합은 다음 단계에서 이
파일에 추가한다."""

import json

from hcr_mcp.company_report.news import collector as news_collector
from hcr_mcp.storage import Storage


async def collect_and_save_news(
    storage: Storage,
    company_name: str,
    industry_keyword: str | None,
    ceo_name: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """회사 이슈 + (있으면) 산업 동향 뉴스를 수집한다. 원문(본문 포함 기사 목록)은 그룹핑·임베딩
    등 선별 단계가 시작되기 전, 검색 라운드마다 스크래핑 직후 바로 로컬에 저장된다(on_raw_ready,
    news_collector._collect_issues 참고) — 선별 단계가 실패해도 이미 저장된 원문은 남는다.
    반환: (회사 이슈 토픽 목록, 산업 동향 토픽 목록) — 둘 다 이후 base 리포트 합성 프롬프트
    (company_report/prompts.py)의 입력으로 쓰인다."""
    company_topics, _ = await news_collector.collect_recent_issues(
        company_name,
        lambda articles: _save_raw_news(storage, company_name, "news_company_raw.json", articles),
        ceo_name,
    )

    industry_topics: list[dict] = []
    if industry_keyword:
        industry_topics, _ = await news_collector.collect_industry_trend(
            industry_keyword,
            lambda articles: _save_raw_news(storage, company_name, "news_industry_raw.json", articles),
        )

    return company_topics, industry_topics


def _save_raw_news(storage: Storage, company_name: str, filename: str, raw_issues: list[dict]) -> None:
    content = json.dumps(raw_issues, ensure_ascii=False, indent=2).encode("utf-8")
    storage.save_raw("company_report", company_name, filename, content)
