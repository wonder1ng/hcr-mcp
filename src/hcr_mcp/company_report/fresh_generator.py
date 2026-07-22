"""base 리포트(CompanyReportBase) LLM 합성 — DART/홈페이지/채용사이트 기업정보/뉴스 요약
4개 소스를 모아 prompts.py의 SYNTHESIS_SYSTEM/HUMAN으로 한 번에 합성한다.

DART 조회는 홈페이지 크롤링 결과(사업자등록번호·대표자명)로 이름만으로는 애매한 후보를
검증하므로(dart_collector.get_corp_code의 known_biz_no/known_ceo_name) 홈페이지 크롤링이 끝난
뒤에만 실행할 수 있다 — 이 두 단계만 순서대로 묶고(_collect_profile_and_dart), 그와 무관한
채용사이트 기업정보 수집은 asyncio.gather로 그 체인 전체와 병렬 실행한다(분기 병렬: 의존관계가
있는 부분만 체인으로 묶고 나머지는 동시에 돌린다).

news_summary는 이 함수의 책임이 아니다 — report_builder.collect_and_save_news가 이미 별도로
수집·저장하므로, 그 결과를 호출자가 그대로 넘겨받아 전달한다(여기서 다시 수집하면 중복 호출).
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from hcr_mcp import llm_client
from hcr_mcp.company_report import dart_collector
from hcr_mcp.company_report.company_profile_collector import collect_company_profile
from hcr_mcp.company_report.prompts import SYNTHESIS_HUMAN, SYNTHESIS_SYSTEM
from hcr_mcp.company_report.schemas import CompanyReportBase
from hcr_mcp.job_posting.site_profile_collector import collect_recruit_site_profile

_NO_DATA = "데이터 없음"


def _fmt(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False) if data else _NO_DATA


async def _collect_profile_and_dart(
    company_name: str, dart_api_key: str | None, cache_dir: Path
) -> tuple[dict, dict | None]:
    """홈페이지 크롤링 → (DART 키가 있으면) 그 결과의 사업자번호/대표자명으로 DART 조회.
    dart_collector가 이름만으로는 애매한 후보를 이 정보로 검증하므로 반드시 이 순서로만
    실행할 수 있다(dart_collector.get_corp_code 참고)."""
    company_profile = await collect_company_profile(company_name)
    if not dart_api_key:
        return company_profile, None

    known_biz_no = company_profile.get("biz_reg_no") if company_profile.get("crawl_success") else None
    known_ceo_name = company_profile.get("ceo_name") if company_profile.get("crawl_success") else None
    dart_data = await dart_collector.collect_dart_data(
        dart_api_key, company_name, cache_dir, known_biz_no=known_biz_no, known_ceo_name=known_ceo_name
    )
    return company_profile, dart_data


async def generate_base_report(
    company_name: str,
    dart_api_key: str | None,
    cache_dir: Path,
    news_summary: dict[str, Any] | None = None,
    job_posting_url: str | None = None,
    company_info_url: str | None = None,
    company_info_screenshot_paths: list[str | Path] | None = None,
) -> CompanyReportBase:
    """DART(선택)+홈페이지+채용사이트 기업정보(선택)+뉴스 요약(선택)을 모아 LLM으로 base
    리포트를 합성한다. job_posting_url이 없으면 채용사이트 기업정보 수집을 건너뛴다
    (company_info_url/screenshot만으로는 원 공고 링크를 못 찾아 기업정보 페이지 자동 추적이
    불가능하므로)."""
    profile_and_dart = _collect_profile_and_dart(company_name, dart_api_key, cache_dir)

    if job_posting_url:
        (company_profile, dart_data), recruit_site = await asyncio.gather(
            profile_and_dart,
            collect_recruit_site_profile(job_posting_url, company_info_url, company_info_screenshot_paths),
        )
    else:
        company_profile, dart_data = await profile_and_dart
        recruit_site = None

    chain = llm_client.structured_chain(SYNTHESIS_SYSTEM, SYNTHESIS_HUMAN, CompanyReportBase)
    return await llm_client.safe_ainvoke(
        chain,
        {
            "company_name": company_name,
            "dart_data_json": _fmt(dart_data),
            "news_summary_json": _fmt(news_summary),
            "company_profile_json": _fmt(company_profile if company_profile.get("crawl_success") else None),
            "recruit_site_json": _fmt(recruit_site),
        },
    )
