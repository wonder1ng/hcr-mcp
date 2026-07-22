"""회사 분석 보고서 조립 — base(DART+홈페이지+채용사이트+LLM 합성) + 뉴스(회사 이슈/산업 동향)를
병합해 schemas.py의 CompanyReportBase 형태로 최종 report.json을 만든다.

뉴스 원문 수집·저장(collect_and_save_news)을 base 합성보다 먼저 만든다 — 스크래핑으로 확보한
기사 원문은 재수집 비용이 가장 크고 손실 위험이 큰 데이터라, 다른 조립 단계보다 먼저 로컬에
안전하게 남겨야 한다(우선순위 지시, notes/phase2_plan.md 참고). 그래서 news_collector의
그룹핑/임베딩/요약(선별 단계, 실패 가능성 있음) 결과를 기다렸다가 저장하지 않고, 매 검색
라운드 스크래핑 직후(on_raw_ready 콜백)마다 즉시 저장한다 — 선별 단계가 도중에 실패해도
이미 스크래핑된 원문은 남는다. base 리포트 합성·최종 병합은 다음 단계에서 이 파일에 추가한다."""

import asyncio
import json

from hcr_mcp import llm_client
from hcr_mcp.company_report import competitor_finder
from hcr_mcp.company_report.news import collector as news_collector
from hcr_mcp.storage import Storage


async def collect_and_save_news(
    storage: Storage,
    company_name: str,
    industry_keywords: list[str],
    job_title: str | None = None,
    ceo_name: str | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """회사 이슈 + (있으면) 산업 동향 + 경쟁사 이슈 + (있으면) 직무 트렌드를 전부 동시에
    수집한다 — 서로 결과를 참조하지 않는 독립적인 검색이라 asyncio.gather로 병렬 실행(순차
    실행 대비 전체 소요 시간을 가장 오래 걸리는 하나 수준으로 줄임). 원문(본문 포함 기사 목록)은
    각자 그룹핑·임베딩 등 선별 단계가 시작되기 전, 검색 라운드/후보마다 스크래핑 직후 바로
    로컬에 저장된다(on_raw_ready, news_collector._collect_issues/_candidate_issues 참고) —
    선별 단계가 실패해도 이미 저장된 원문은 남는다.

    industry_keywords: industry_keyword.derive_industry_keywords가 뽑은 사업 영역 목록(중요도순,
    최대 5개). 경쟁사 탐색(competitor_finder)은 사업 영역마다 따로 검색하지만(query fan-out),
    산업 동향 검색(collect_industry_trend)은 아직 단일 키워드만 받아 가장 중요한 첫 번째만
    넘긴다(다중 키워드 확장은 이번 변경 범위 밖).
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
            industry_keywords[0],
            lambda articles: _save_raw_news(storage, company_name, "news_industry_raw.json", articles),
        )
        if industry_keywords else _empty_topics()
    )
    tasks.append(
        competitor_finder.collect_competitor_issues(
            company_name, industry_keywords,
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

    (company_topics, _), (industry_topics, _), (competitor_topics, related_institution_topics), (job_topics, _) = (
        await asyncio.gather(*tasks)
    )
    # 경쟁사 분류 단계(competitor_finder._classify_candidate)에서 "경쟁 관계는 아니지만 산업
    # 생태계 관련"(공공기관·데이터 제공처 등)으로 분류된 대상은 버리지 않고 산업 동향에 합류—
    # 산업 동향 파악이라는 원래 목적(경쟁사 탐색 자체가 이걸 위한 수단)에 더 맞는다.
    industry_topics = industry_topics + related_institution_topics

    # 로컬 RAG 조회용 — 각 이슈에 issue_title+gist 임베딩을 붙인다(벡터 DB 없이 저장된 리포트
    # JSON에서 직접 유사도 계산). 4개 토픽 리스트는 서로 무관해 병렬로 처리.
    await asyncio.gather(
        _embed_topic_issues(company_topics),
        _embed_topic_issues(industry_topics),
        _embed_topic_issues(competitor_topics),
        _embed_topic_issues(job_topics),
    )

    # 그룹핑·분류·중요도재평가·임베딩까지 끝난 최종 토픽(가공 데이터)도 저장한다 — 지금까지는
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
    """industry_keywords/job_title이 없을 때 나머지와 같은 tuple[list, list] 형태를 맞추기 위한
    빈 결과 — asyncio.gather에 조건부로 다른 코루틴을 섞어 넣을 수 있게 한다."""
    return [], []


async def _embed_topic_issues(topics: list[dict]) -> None:
    """토픽 버킷 리스트(_group_into_topics 결과, {"issues": [...]}) 안의 이슈마다
    issue_title+gist 임베딩을 "embedding" 필드로 붙인다(in-place) — 나중에 벡터 DB 없이 저장된
    리포트 JSON에서 직접 코사인 유사도로 top-k 조회하기 위함. embed_batch가 이미 OpenAI
    요청/입력 토큰 한도를 알아서 나눠 처리하므로 여기서는 텍스트만 모아 한 번에 넘긴다."""
    issues = [issue for topic in topics for issue in topic["issues"]]
    if not issues:
        return
    texts = [f"{issue['issue_title']} {issue['gist']}".strip() for issue in issues]
    vectors = await llm_client.embed_batch(texts)
    for issue, vector in zip(issues, vectors):
        issue["embedding"] = vector


def _save_raw_news(storage: Storage, company_name: str, filename: str, raw_issues: list[dict]) -> None:
    content = json.dumps(raw_issues, ensure_ascii=False, indent=2).encode("utf-8")
    storage.save_raw("company_report", company_name, filename, content)
