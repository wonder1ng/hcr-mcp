"""적합성 분석 서비스 — 5단계 Pipeline (hcr-backend/app/analysis/service.py copy-adapt).

원본과의 차이:
- MariaDB/MongoDB 완전 제거. 회사 데이터는 Phase 2가 만든 로컬 report.json(dict)을 그대로 받는다.
  없으면 빈 dict로 진행 — CompanyProfile 관련 매칭 항목은 자동으로 걸러진다(_has_dim).
- fit_analyses 캐시/업서트/동시요청 dedup/user_id/docs_updated_at 버전관리 없음 — 단일 로컬 실행 1회성 호출.
- job/candidate에 Mongo _id 대신 로컬 상수 문자열을 evidence 참조 ID로 사용.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from hcr_mcp import llm_client
from hcr_mcp.fit.prompts import (
    CANDIDATE_PROFILE_SYSTEM, CANDIDATE_PROFILE_HUMAN,
    JOB_PROFILE_SYSTEM, JOB_PROFILE_HUMAN,
    COMPANY_PROFILE_SYSTEM, COMPANY_PROFILE_HUMAN,
    REQUIREMENT_MATCHER_SYSTEM, REQUIREMENT_MATCHER_HUMAN,
    COMPANY_MATCHER_SYSTEM, COMPANY_MATCHER_HUMAN,
    REPORT_GENERATOR_SYSTEM, REPORT_GENERATOR_HUMAN,
)
from hcr_mcp.fit.schemas import (
    CandidateProfile, JobProfile, CompanyProfile,
    LLMJobMatchingResult, LLMCompanyMatchingResult, LLMReportSummary,
    EvidenceRef, JobMatch, CompanyMatch, CategorySummary,
)

_CATEGORY_MAP = {
    "required": "자격요건",
    "preferred": "우대사항",
    "responsibility": "주요업무",
    "tech_tool": "기술·도구",
    "career": "경력사항",
    "education": "학력사항",
    "industry_domain": "산업 및 사업 분야",
    "culture": "인재상 및 조직문화",
    "talent_values": "인재상 및 조직문화",
}


# ── 데이터 준비 ───────────────────────────────────────────────────────

def _load_company_data(report: dict[str, Any] | None) -> dict[str, Any]:
    """Phase 2 company_report/{company}/report.json → CompanyProfile 프롬프트 입력 dict.
    report가 없으면(Phase 2 미실행) 빈 dict — CompanyProfile은 비어있는 채로 진행되고
    company_llm.items의 _has_dim 필터가 해당 매칭 항목을 자동으로 제거한다.
    """
    if not report:
        return {}

    def _summary(field: str) -> Any:
        v = report.get(field)
        return v.get("summary") if isinstance(v, dict) else v

    def _texts(field: str) -> list[str]:
        return [i["text"] for i in (report.get(field) or []) if isinstance(i, dict) and i.get("text")]

    data = {
        "company_name": report.get("company_name"),
        "industry_status": report.get("industry_status"),
        "financial_analysis": _summary("financial_analysis"),
        "growth_potential": _summary("growth_potential"),
        "jobplanet_review_summary": _summary("jobplanet_review_summary"),
        "swot_strengths": _texts("swot_strengths"),
        "swot_weaknesses": _texts("swot_weaknesses"),
        "swot_opportunities": _texts("swot_opportunities"),
        "swot_threats": _texts("swot_threats"),
        "key_points": _texts("key_points"),
    }
    return {k: v for k, v in data.items() if v}


# ── Stage 1: Profile 생성 ─────────────────────────────────────────────

async def _gen_candidate_profile(user_doc_json: str) -> CandidateProfile:
    return await llm_client.safe_ainvoke(
        llm_client.structured_chain(CANDIDATE_PROFILE_SYSTEM, CANDIDATE_PROFILE_HUMAN, CandidateProfile),
        {"user_doc_json": user_doc_json},
    )


async def _gen_job_profile(job_doc_json: str) -> JobProfile:
    return await llm_client.safe_ainvoke(
        llm_client.structured_chain(JOB_PROFILE_SYSTEM, JOB_PROFILE_HUMAN, JobProfile),
        {"job_doc_json": job_doc_json},
    )


async def _gen_company_profile(company_data_json: str) -> CompanyProfile:
    return await llm_client.safe_ainvoke(
        llm_client.structured_chain(COMPANY_PROFILE_SYSTEM, COMPANY_PROFILE_HUMAN, CompanyProfile),
        {"company_data_json": company_data_json},
    )


# ── Stage 2: 매칭 ─────────────────────────────────────────────────────

async def _match_job(candidate: CandidateProfile, job: JobProfile) -> LLMJobMatchingResult:
    return await llm_client.safe_ainvoke(
        llm_client.structured_chain(REQUIREMENT_MATCHER_SYSTEM, REQUIREMENT_MATCHER_HUMAN, LLMJobMatchingResult),
        {
            "candidate_profile_json": candidate.model_dump_json(indent=2),
            "job_profile_json": job.model_dump_json(indent=2),
        },
    )


async def _match_company(candidate: CandidateProfile, company: CompanyProfile) -> LLMCompanyMatchingResult:
    return await llm_client.safe_ainvoke(
        llm_client.structured_chain(COMPANY_MATCHER_SYSTEM, COMPANY_MATCHER_HUMAN, LLMCompanyMatchingResult),
        {
            "candidate_profile_json": candidate.model_dump_json(indent=2),
            "company_profile_json": company.model_dump_json(indent=2),
        },
    )


def _build_evidence_ref(path: str | None, llm_excerpt: str | None, profile: CandidateProfile, cp_id: str) -> EvidenceRef:
    feat = None
    if path:
        m = re.match(r"^(skills|experiences|education|certifications|awards)\[(\d+)\]$", path)
        if m:
            section, idx = m.group(1), int(m.group(2))
            features = getattr(profile, section, None)
            if features and idx < len(features):
                feat = features[idx]
    return EvidenceRef(
        doc_id=cp_id,
        field=path if feat else None,
        feature_name=feat.name if feat else None,
        excerpt=feat.evidence if feat else llm_excerpt,
        source=feat.source if feat else None,
    )


def _build_job_matches(
    llm_result: LLMJobMatchingResult, job_posting_id: str, candidate_profile: CandidateProfile, cp_id: str
) -> list[JobMatch]:
    return [
        JobMatch(
            job_posting_id=job_posting_id,
            match_target_type=item.match_target_type,
            match_target_text=item.match_target_text,
            match_target_evidence=item.match_target_evidence,
            matched=item.matched,
            candidate_profile_id=cp_id,
            candidate_evidence=_build_evidence_ref(item.candidate_feature_path, item.candidate_evidence_excerpt, candidate_profile, cp_id),
            reasoning=item.reasoning,
        )
        for item in llm_result.items
    ]


def _build_company_matches(
    llm_result: LLMCompanyMatchingResult, co_id: str, candidate_profile: CandidateProfile, cp_id: str
) -> list[CompanyMatch]:
    return [
        CompanyMatch(
            company_profile_id=co_id,
            dimension=item.dimension,
            criterion_text=item.criterion_text,
            criterion_evidence=item.criterion_evidence,
            matched=item.matched,
            candidate_profile_id=cp_id,
            candidate_evidence=_build_evidence_ref(item.candidate_feature_path, item.candidate_evidence_excerpt, candidate_profile, cp_id),
            reasoning=item.reasoning,
        )
        for item in llm_result.items
    ]


# ── Stage 3: 리포트 ───────────────────────────────────────────────────

def _build_category_summary(job_matches: list[JobMatch], company_matches: list[CompanyMatch]) -> list[CategorySummary]:
    counts: dict[str, dict[str, int]] = {cat: {"total": 0, "matched": 0} for cat in _CATEGORY_MAP.values()}
    for m in job_matches:
        cat = _CATEGORY_MAP.get(m.match_target_type)
        if cat:
            counts[cat]["total"] += 1
            if m.matched:
                counts[cat]["matched"] += 1
    for m in company_matches:
        cat = _CATEGORY_MAP.get(m.dimension)
        if cat:
            counts[cat]["total"] += 1
            if m.matched:
                counts[cat]["matched"] += 1
    return [CategorySummary(category=cat, total=v["total"], matched=v["matched"]) for cat, v in counts.items() if v["total"] > 0]


def _format_matches_text(job_matches: list[JobMatch], company_matches: list[CompanyMatch]) -> tuple[str, str]:
    def _fmt(matches: list[Any]) -> str:
        lines = []
        for m in matches:
            label = _CATEGORY_MAP.get(getattr(m, "match_target_type", None) or getattr(m, "dimension", ""), "기타")
            text = getattr(m, "match_target_text", None) or getattr(m, "criterion_text", "")
            lines.append(f"[{label}] {'✓' if m.matched else '✗'} {text}")
            if m.matched and m.candidate_evidence.excerpt:
                lines.append(f"  근거: {m.candidate_evidence.excerpt}")
            if m.reasoning:
                lines.append(f"  판단: {m.reasoning}")
        return "\n".join(lines)

    return _fmt(job_matches), _fmt(company_matches)


async def _gen_report(job_matches: list[JobMatch], company_matches: list[CompanyMatch]) -> LLMReportSummary:
    job_text, company_text = _format_matches_text(job_matches, company_matches)
    return await llm_client.safe_ainvoke(
        llm_client.structured_chain(REPORT_GENERATOR_SYSTEM, REPORT_GENERATOR_HUMAN, LLMReportSummary),
        {"job_matches_text": job_text, "company_matches_text": company_text},
    )


# ── 진입점 ────────────────────────────────────────────────────────────

async def analyze_fit(
    candidate_doc: dict[str, Any],
    job_doc_json: str,
    company_report: dict[str, Any] | None,
) -> dict[str, Any]:
    """이력서(candidate_doc) × 공고(job_doc_json) × 회사 리포트(company_report, 선택) → 적합도 분석 결과."""
    company_data = _load_company_data(company_report)
    user_doc_json = json.dumps({"resume": candidate_doc}, ensure_ascii=False, default=str)
    company_data_json = json.dumps(company_data, ensure_ascii=False)

    candidate_profile, job_profile, company_profile = await asyncio.gather(
        _gen_candidate_profile(user_doc_json),
        _gen_job_profile(job_doc_json),
        _gen_company_profile(company_data_json),
    )

    job_llm, company_llm = await asyncio.gather(
        _match_job(candidate_profile, job_profile),
        _match_company(candidate_profile, company_profile),
    )

    # CompanyProfile에 실제 데이터가 없는 dimension은 제거 — 없으면 LLM 할루시네이션으로 항목이 생겨 카운트가 왜곡됨
    _has_dim = {
        "industry_domain": bool(company_profile.industry_domain),
        "culture": bool(company_profile.culture),
        "talent_values": bool(company_profile.talent_values),
    }
    company_llm.items = [i for i in company_llm.items if _has_dim.get(i.dimension, True)]

    cp_id, co_id = "candidate", "company"
    job_matches = _build_job_matches(job_llm, "job", candidate_profile, cp_id)
    company_matches = _build_company_matches(company_llm, co_id, candidate_profile, cp_id)
    category_summary = _build_category_summary(job_matches, company_matches)
    summary = await _gen_report(job_matches, company_matches)

    return {
        "job_title": job_profile.job_title,
        "company_name": company_data.get("company_name") or company_profile.company_name,
        "overall_summary": summary.overall_summary,
        "job_matches": [m.model_dump() for m in job_matches],
        "company_matches": [m.model_dump() for m in company_matches],
        "category_summary": [c.model_dump() for c in category_summary],
        "strengths": summary.strengths,
        "improvements": summary.improvements,
        "recommendations": summary.recommendations,
    }
