import json
from typing import Literal

from hcr_mcp.fit import resume_collector, service
from hcr_mcp.job_posting import collector as job_posting_collector
from hcr_mcp.server import get_storage, mcp


@mcp.tool()
async def analyze_fit(
    resume_path: str,
    job_title: str,
    job_url: str | None = None,
    job_screenshot_paths: list[str] | None = None,
    company_name: str | None = None,
    storage_level: Literal["none", "results", "raw"] | None = None,
) -> dict:
    """이력서와 채용 공고를 비교해 적합도 분석 보고서를 생성합니다.

    company_name을 주면 회사 분석 보고서(generate_company_report 툴로 미리 생성된 것이 있을 때)를
    함께 반영해 기업 적합도까지 분석합니다 — 없어도 직무 적합도 분석은 정상 동작합니다.
    """
    candidate_doc = await resume_collector.parse_resume(resume_path)
    posting = await job_posting_collector.collect_job_posting(job_title, get_storage(), job_url, job_screenshot_paths)
    job_doc_json = json.dumps({"target_job_title": job_title, **posting.model_dump()}, ensure_ascii=False)

    company_report = None
    if company_name:
        company_report = get_storage().latest_report("company_report", company_name)

    result = await service.analyze_fit(candidate_doc, job_doc_json, company_report)

    saved_path = get_storage().save_report("fit", company_name or job_title, result, storage_level)
    if saved_path:
        result["_saved_path"] = str(saved_path)
    return result
