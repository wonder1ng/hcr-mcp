import json

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
) -> dict:
    """이력서와 채용 공고를 비교해 적합도 분석 보고서를 생성합니다.

    company_name을 주면 회사 분석 보고서(generate_company_report 툴로 미리 생성된 것이 있을 때)를
    함께 반영해 기업 적합도까지 분석합니다 — 없어도 직무 적합도 분석은 정상 동작합니다.

    이력서·공고 원문·분석 결과는 항상 로컬에 저장됩니다(재분석 시 재사용) — 저장 여부를
    호출마다 선택할 수 없고, 최초 호출 시에만 결과의 `_storage_notice` 필드로 안내합니다.
    """
    storage = get_storage()
    notice = storage.consume_first_use_notice()

    candidate_doc = await resume_collector.parse_resume(resume_path)
    posting = await job_posting_collector.collect_job_posting(job_title, storage, job_url, job_screenshot_paths)
    job_doc_json = json.dumps({"target_job_title": job_title, **posting.model_dump()}, ensure_ascii=False)

    company_report = None
    if company_name:
        company_report = storage.latest_report("company_report", company_name)

    result = await service.analyze_fit(candidate_doc, job_doc_json, company_report)

    saved_path = storage.save_report("fit", company_name or job_title, result)
    result["_saved_path"] = str(saved_path)
    if notice:
        result["_storage_notice"] = notice
    return result
