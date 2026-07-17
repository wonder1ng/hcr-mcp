import json

from hcr_mcp.errors import HcrMcpError
from hcr_mcp.fit import resume_collector, service
from hcr_mcp.job_posting import collector as job_posting_collector
from hcr_mcp.server import get_storage, mcp


def _resolve_job_title(job_names: list[str]) -> tuple[str, None] | tuple[None, dict]:
    """job_title 없이 호출됐을 때 posting.jobs에서 진행할 job_title을 정한다.
    반환: (job_title, None) 확정, 또는 (None, 조기반환용 dict) 사용자 선택 필요/불가."""
    if len(job_names) > 1:
        return None, {
            "message": "공고에 모집분야가 여러 개 있습니다. 아래 job_titles 중 하나를 골라 "
            "analyze_fit을 job_title 인자와 함께 다시 호출해주세요.",
            "job_titles": job_names,
        }
    if not job_names:
        raise HcrMcpError("공고에서 모집분야를 찾지 못했습니다. job_title을 직접 입력해 다시 호출해주세요.")
    return job_names[0], None


@mcp.tool()
async def analyze_fit(
    resume_path: str,
    job_url: str | None = None,
    job_title: str | None = None,
    job_screenshot_paths: list[str] | None = None,
    company_name: str | None = None,
) -> dict:
    """이력서와 채용 공고를 비교해 적합도 분석 보고서를 생성합니다.

    job_title을 생략하면 공고에서 모집분야를 자동으로 읽어 진행합니다 — 모집분야가 1개뿐이면
    그대로 진행하고, 2개 이상 혼재돼 있으면(예: 한 공고에 "백엔드 개발자"/"프론트엔드 개발자"가
    같이 있는 경우) 분석을 진행하지 않고 모집분야 목록만 반환하니, 그중 하나를 job_title 인자로
    지정해 다시 호출해주세요.

    company_name을 주면 회사 분석 보고서(generate_company_report 툴로 미리 생성된 것이 있을 때)를
    함께 반영해 기업 적합도까지 분석합니다 — 없어도 직무 적합도 분석은 정상 동작합니다.

    이력서·공고 원문·분석 결과는 항상 로컬에 저장됩니다(재분석 시 재사용) — 저장 여부를
    호출마다 선택할 수 없고, 최초 호출 시에만 결과의 `_storage_notice` 필드로 안내합니다.
    """
    storage = get_storage()
    notice = storage.consume_first_use_notice()

    posting = await job_posting_collector.collect_job_posting(job_title, storage, job_url, job_screenshot_paths)

    if job_title is None:
        job_names = [j.job_name for j in posting.jobs if j.job_name]
        job_title, early_result = _resolve_job_title(job_names)
        if early_result is not None:
            if notice:
                early_result["_storage_notice"] = notice
            return early_result

    candidate_doc = await resume_collector.parse_resume(resume_path)
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
