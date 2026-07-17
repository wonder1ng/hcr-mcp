"""회사 분석 보고서 조립 — base(DART+홈페이지+채용사이트+LLM 합성) + 뉴스(회사 이슈/산업 동향)를
병합해 schemas.py의 CompanyReportBase 형태로 최종 report.json을 만든다.

뉴스 원문 수집·저장(collect_and_save_news)을 base 합성보다 먼저 만든다 — 스크래핑으로 확보한
기사 원문은 재수집 비용이 가장 크고 손실 위험이 큰 데이터라, 다른 조립 단계보다 먼저 로컬에
안전하게 남겨야 한다(우선순위 지시, notes/phase2_plan.md 참고). 그래서 news_collector의
그룹핑/임베딩/요약(선별 단계, 실패 가능성 있음) 결과를 기다렸다가 저장하지 않고, 매 검색
라운드 스크래핑 직후(on_raw_ready 콜백)마다 즉시 저장한다 — 선별 단계가 도중에 실패해도
이미 스크래핑된 원문은 남는다. base 리포트 합성·임베딩 저장·최종 병합은 다음 단계에서 이
파일에 추가한다."""

import asyncio
import json

from hcr_mcp.company_report import competitor_finder
from hcr_mcp.company_report.news import collector as news_collector
from hcr_mcp.storage import Storage


async def collect_and_save_news(
    storage: Storage,
    company_name: str,
    industry_keyword: str | None,
    job_title: str | None = None,
    ceo_name: str | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """회사 이슈 + (있으면) 산업 동향 + 경쟁사 이슈 + (있으면) 직무 트렌드를 전부 동시에
    수집한다 — 서로 결과를 참조하지 않는 독립적인 검색이라 asyncio.gather로 병렬 실행(순차
    실행 대비 전체 소요 시간을 가장 오래 걸리는 하나 수준으로 줄임). 원문(본문 포함 기사 목록)은
    각자 그룹핑·임베딩 등 선별 단계가 시작되기 전, 검색 라운드/후보마다 스크래핑 직후 바로
    로컬에 저장된다(on_raw_ready, news_collector._collect_issues/_candidate_issues 참고) —
    선별 단계가 실패해도 이미 저장된 원문은 남는다.
    반환: (회사 이슈 토픽, 산업 동향 토픽, 경쟁사 이슈 토픽, 직무 트렌드 토픽) — 전부 이후 base
    리포트 합성 프롬프트(company_report/prompts.py)의 입력으로 쓰인다."""
    tasks = [
        news_collector.collect_recent_issues(
            company_name,
            lambda articles: _save_raw_news(storage, company_name, "news_company_raw.json", articles),
            ceo_name,
        ),
    ]
    tasks.append(
        news_collector.collect_industry_trend(
            industry_keyword,
            lambda articles: _save_raw_news(storage, company_name, "news_industry_raw.json", articles),
        )
        if industry_keyword else _empty_topics()
    )
    tasks.append(
        competitor_finder.collect_competitor_issues(
            company_name, industry_keyword,
            lambda articles: _save_raw_news(storage, company_name, "news_competitor_raw.json", articles),
        )
    )
    tasks.append(
        news_collector.collect_job_trend(
            job_title,
            lambda articles: _save_raw_news(storage, company_name, "news_job_trend_raw.json", articles),
        )
        if job_title else _empty_topics()
    )

    (company_topics, _), (industry_topics, _), (competitor_topics, _), (job_topics, _) = await asyncio.gather(*tasks)

    # 그룹핑·분류·중요도재평가까지 끝난 최종 토픽(가공 데이터)도 저장한다 — 지금까지는
    # on_raw_ready로 스크래핑 직후 원문만 저장되고, 이 최종 결과물은 어디에도 저장되지
    # 않았음(실측 확인: report_builder.py 전체 git 히스토리에 save_report 호출 자체가 없었음).
    # "수집·가공 데이터는 전부 저장"이라는 이 프로젝트 원칙에 맞춰 추가.
    storage.save_report(
        "company_report",
        company_name,
        {
            "company_topics": company_topics,
            "industry_topics": industry_topics,
            "competitor_topics": competitor_topics,
            "job_topics": job_topics,
        },
    )

    return company_topics, industry_topics, competitor_topics, job_topics


async def _empty_topics() -> tuple[list[dict], list[dict]]:
    """industry_keyword/job_title이 없을 때 나머지와 같은 tuple[list, list] 형태를 맞추기 위한
    빈 결과 — asyncio.gather에 조건부로 다른 코루틴을 섞어 넣을 수 있게 한다."""
    return [], []


def _save_raw_news(storage: Storage, company_name: str, filename: str, raw_issues: list[dict]) -> None:
    content = json.dumps(raw_issues, ensure_ascii=False, indent=2).encode("utf-8")
    storage.save_raw("company_report", company_name, filename, content)
