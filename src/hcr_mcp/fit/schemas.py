"""적합성 분석 스키마 — Profile 중심 설계 (hcr-backend/app/analysis/schemas.py 원본 그대로, DB 결합 없음).

흐름:
  Stage 1a/1b/1c (병렬): CandidateProfile / JobProfile / CompanyProfile 생성
  Stage 2a/2b (병렬):    Candidate vs Job 매칭 / Candidate vs Company 매칭
  Stage 3:               카테고리별 집계 + 강점 · 보완점 · 개선 방안 생성

LLM 역할: 구조화 · 매칭 · 근거 생성만. 점수 · 합격 판정 없음.
None = 해당 없음 / 원본에 데이터 없음.
"""

from pydantic import BaseModel, Field


# ─── Stage 1: Profile 스키마 (LLM 출력) ──────────────────────────────

class Feature(BaseModel):
    name: str = Field(description="특성명 (예: 'Python 개발 경험', 'Microsoft Excel')")
    evidence: str = Field(description="원본 문서의 해당 내용을 그대로 인용한 텍스트")
    source: str = Field(description="원본 JSON 내 경로 (예: 'resume.career[0]', 'work_experience.work_experience[0].projects[1]')")


class CandidateProfile(BaseModel):
    skills: list[Feature] | None = Field(None, description="기술·도구 역량")
    experiences: list[Feature] | None = Field(None, description="경력·프로젝트 경험")
    education: list[Feature] | None = Field(None, description="학력(resume.school) 및 부트캠프·강의·연수 등 학위 외 교육 이수 전체")
    certifications: list[Feature] | None = Field(None, description="자격증·수료증")
    awards: list[Feature] | None = Field(None, description="수상·표창·장학금 등 수상경력 (어느 필드에서 언급되더라도 추출)")


class JobRequirement(BaseModel):
    index: int = Field(description="원본 배열 내 위치 (0부터)")
    text: str
    evidence: str | None = Field(None, description="채용공고 원문 발췌")


class JobProfile(BaseModel):
    job_title: str
    responsibilities: list[Feature] | None = Field(None, description="주요 업무 목록")
    tech_tools: list[Feature] | None = Field(
        None,
        description=(
            "공고 전체(주요업무·자격요건·우대사항)에서 언급된 모든 도구·기술. "
            "워드·엑셀·파워포인트 등 오피스, 피그마 등 디자인, Python·AWS 등 개발 도구 등 종류 무관. "
            "source에 언급 위치 기록 (예: 'requirements[1]', 'responsibilities[2]')"
        ),
    )
    required: list[JobRequirement] | None = Field(None, description="자격요건 (requirements[] 배열) — 경력·학력 제외한 나머지")
    preferred: list[JobRequirement] | None = Field(None, description="우대사항 (preferred_qualifications[] 배열)")
    career: list[JobRequirement] | None = Field(
        None,
        description=(
            "근무 경력 요건만. 신입 가능·경력무관·경력 O년 이상 등 근무 기간·형태 관련 내용만. "
            "학력·기술·자격증·역량 내용은 포함하지 않음. 공고 어느 위치에 있어도 근무 경력이면 여기로. 데이터 없으면 null."
        ),
    )
    education: list[JobRequirement] | None = Field(
        None,
        description=(
            "학력 요건. 공고 어느 위치에 있든 학력 관련 내용을 모두 여기에 추출. "
            "고졸·학력무관·대졸 이상 등 모두 포함. 데이터 없으면 null."
        ),
    )


class CompanyFeature(BaseModel):
    aspect: str = Field(description="세부 항목명 (예: 'SaaS 도메인', '수평적 조직 문화')")
    description: str
    evidence: str | None = Field(None, description="원본 보고서 원문 발췌")


class CompanyProfile(BaseModel):
    company_name: str | None = None
    industry_domain: list[CompanyFeature] | None = Field(
        None, description="회사가 속한 산업·사업 분야·서비스 도메인 (industry, business_description, main_products_services 참조)"
    )
    culture: list[CompanyFeature] | None = Field(
        None, description="조직 문화·근무 환경 (jobplanet_review_summary, key_points 참조)"
    )
    talent_values: list[CompanyFeature] | None = Field(
        None, description="인재상·핵심 가치 (ceo_message, key_points, swot_strengths 참조)"
    )


# ─── Stage 2a: Job Matching LLM 출력 ─────────────────────────────────

class LLMJobMatchItem(BaseModel):
    match_target_type: str = Field(description="'required' | 'preferred' | 'responsibility' | 'tech_tool' | 'career' | 'education'")
    match_target_text: str = Field(description="평가 기준 원문")
    match_target_evidence: str | None = Field(
        None, description="채용공고에서 이 요건을 설정한 원문 (JobRequirement.evidence 또는 Feature.evidence 그대로 인용)"
    )
    matched: bool
    candidate_feature_path: str | None = Field(
        None, description="매칭된 CandidateProfile Feature 경로 (예: 'skills[0]', 'experiences[2]', 'education[1]', 'certifications[0]', 'awards[0]'). 미매칭 시 null"
    )
    candidate_evidence_excerpt: str | None = Field(
        None, description="매칭된 Feature의 evidence 텍스트 원문 인용. 미매칭 시 null"
    )
    reasoning: str | None = None


class LLMJobMatchingResult(BaseModel):
    items: list[LLMJobMatchItem]


# ─── Stage 2b: Company Matching LLM 출력 ─────────────────────────────

class LLMCompanyMatchItem(BaseModel):
    dimension: str = Field(description="'industry_domain' | 'culture' | 'talent_values'")
    criterion_text: str = Field(description="CompanyFeature.description 에코")
    criterion_evidence: str | None = Field(
        None, description="기업 리포트에서 이 기준을 설정한 원문 (CompanyFeature.evidence 그대로 인용)"
    )
    matched: bool
    candidate_feature_path: str | None = Field(
        None, description="매칭된 CandidateProfile Feature 경로. 미매칭 시 null"
    )
    candidate_evidence_excerpt: str | None = Field(
        None, description="매칭된 Feature의 evidence 텍스트 원문 인용. 미매칭 시 null"
    )
    reasoning: str | None = None


class LLMCompanyMatchingResult(BaseModel):
    items: list[LLMCompanyMatchItem]


# ─── Stage 3: Report LLM 출력 ────────────────────────────────────────

class LLMReportSummary(BaseModel):
    overall_summary: str = Field(description="직무·기업 적합도를 포함한 전체 결과 객관적 요약 (2~3문장, 합격/불합격 판정 없음)")
    strengths: list[str] | None = Field(None, description="충족 항목 중 두드러지는 강점 (근거 기반 서술). 강점 1개 = 리스트 항목 1개")
    improvements: list[str] | None = Field(None, description="미충족 항목이 왜 갭인지 항목당 1문장 서술. 행동 지침 없이 갭 사실만")
    recommendations: list[str] | None = Field(None, description="각 갭을 해소하기 위한 구체적 행동 방안. improvements와 1:1 대응")


# ─── 최종 저장/반환 스키마 ─────────────────────────────────────────

class EvidenceRef(BaseModel):
    doc_id: str = Field(description="후보 프로필 식별자")
    field: str | None = Field(None, description="Profile 내 경로 (예: 'skills[0]'). 미매칭 시 null")
    feature_name: str | None = Field(None, description="매칭된 Feature.name. 미매칭 시 null")
    excerpt: str | None = Field(None, description="Feature.evidence 원문. 미매칭 시 null")
    source: str | None = Field(None, description="Feature.source — 원본 문서 내 JSON 경로. 미매칭 시 null")


class JobMatch(BaseModel):
    job_posting_id: str = Field(description="공고 식별자 (로컬 키)")
    match_target_type: str
    match_target_text: str
    match_target_evidence: str | None = None
    matched: bool
    candidate_profile_id: str
    candidate_evidence: EvidenceRef
    reasoning: str | None = None


class CompanyMatch(BaseModel):
    company_profile_id: str
    dimension: str
    criterion_text: str
    criterion_evidence: str | None = None
    matched: bool
    candidate_profile_id: str
    candidate_evidence: EvidenceRef
    reasoning: str | None = None


class CategorySummary(BaseModel):
    category: str = Field(description="'자격요건' | '우대사항' | '주요업무' | '기술·도구' | '경력사항' | '학력사항' | '산업 및 사업 분야' | '인재상 및 조직문화'")
    total: int
    matched: int


class EvidenceReport(BaseModel):
    analysis_id: str
    candidate_profile_id: str
    job_profile_id: str
    company_profile_id: str
    overall_summary: str
    job_matches: list[JobMatch]
    company_matches: list[CompanyMatch]
    category_summary: list[CategorySummary]
    strengths: list[str] | None = None
    improvements: list[str] | None = None
    recommendations: list[str] | None = None
