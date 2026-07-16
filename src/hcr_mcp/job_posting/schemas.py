"""채용공고 구조화 스키마 (HcR/hiring_preprocess/job_schema_v2.json copy-adapt).

원본과의 차이:
- 배치용 추적 필드 제거: raw_meta.source_file/source_row(TSV 행 추적용, 단발 호출엔 의미 없음),
  preprocess_log.original_text_snapshot(LLM 캐시 대조용 — 원문은 storage.save_raw로 이미 통째로 보존).
- raw_meta.ocr_used → vision_used로 개명(EasyOCR이 아니라 llm_client.vision_extract 사용).
- 신규: jobs[].department — 공고에 언급된 팀/본부/사업부명을 원문 그대로 추출(부서→사업분야
  매핑은 안 함, industry_keyword.py 몫). 없으면 null.
- 원본은 OpenAI Responses API strict 모드용이라 모든 필드가 필수(빈 값은 ""/[])였지만, 이
  프로젝트는 LangChain with_structured_output + Pydantic(fit/schemas.py와 동일 패턴)을 쓰므로
  없는 값은 진짜 None으로 표현한다.
"""

from pydantic import BaseModel, Field


class JobCommon(BaseModel):
    education: str | None = Field(None, description="공고 전체 공통 학력")
    major: str | None = Field(None, description="공고 전체 공통 전공")
    preferred: list[str] | None = Field(None, description="공고 전체 모든 직무에 적용되는 공통 우대사항만(특정 직무 우대사항은 여기 넣지 않음)")
    documents: list[str] | None = Field(None, description="공통 제출서류")


class Track(BaseModel):
    requirements: list[str] | None = Field(None, description="이 트랙(신입/경력) 전용 자격요건")
    preferred: list[str] | None = Field(None, description="이 트랙 전용 우대사항")
    responsibilities: list[str] | None = Field(None, description="이 트랙 전용 업무")
    documents: list[str] | None = Field(None, description="이 트랙 전용 제출서류")


class Tracks(BaseModel):
    newcomer: Track | None = Field(None, description="신입 전용 조건")
    experienced: Track | None = Field(None, description="경력 전용 조건")


class JobEntry(BaseModel):
    job_name: str = Field(description="모집분야/직무명")
    department: str | None = Field(None, description="공고에 팀·본부·사업부명이 명시돼 있으면 원문 그대로 추출. 추측 금지, 없으면 null")
    headcount: str | None = Field(None, description="모집인원 원문 표현 그대로 보존(예: '0명'도 미기재가 아니라 원문 그대로)")
    education: str | None = Field(None, description="이 직무의 학력 요건(고졸/전문학사/학사/석사/박사/무관). 자격증·어학점수는 여기 넣지 않음")
    major: str | None = Field(None, description="이 직무의 전공 요건")
    locations: list[str] | None = Field(None, description="근무지")
    responsibilities: list[str] | None = Field(None, description="신입/경력 공통 실제 수행 업무만(자격요건 아님)")
    preferred_common: list[str] | None = Field(None, description="이 직무 신입/경력 모두에 적용되는 우대사항(트랙 전용 우대사항과 중복 금지)")
    tracks: Tracks | None = None


class WorkConditions(BaseModel):
    employment_type: str | None = Field(None, description="고용형태")
    work_type: str | None = Field(None, description="근무형태")
    salary: str | None = None
    benefits: list[str] | None = None
    deadline: str | None = Field(None, description="YYYY-MM-DD, timezone 추정 금지")
    recruit_url: str | None = Field(None, description="실제 지원/공고 URL만. 이메일 접수만 있으면 null")


class RawMeta(BaseModel):
    source_url: str | None = None
    vision_used: bool = False
    llm_used: bool = True
    llm_error: str | None = None


class DroppedField(BaseModel):
    field: str
    original_value: str
    reason: str


class LowConfidence(BaseModel):
    field: str
    original_text: str
    issue: str
    confidence: float = Field(description="0.0~1.0")


class PreprocessLog(BaseModel):
    dropped_fields: list[DroppedField] | None = Field(None, description="원문에는 있었지만 스키마에 넣지 못했거나 버린 값과 이유")
    low_confidence: list[LowConfidence] | None = Field(None, description="분류가 애매하거나 신뢰도가 낮은 판단")
    parse_warnings: list[str] | None = Field(None, description="날짜/위치/인원/제출서류 등 파싱 이슈. 이메일·전화번호처럼 전용 필드가 없는 값도 여기 기록")


class JobPosting(BaseModel):
    """구조화된 채용공고 1건. 모집분야가 여러 개면 jobs[]를 반드시 분리한다(서로 다른
    모집분야의 근무지·자격요건·업무를 섞지 않음)."""

    company_name: str | None = None
    posting_title: str | None = None
    source_site: str | None = None
    source_url: str | None = None
    common: JobCommon
    jobs: list[JobEntry] = Field(default_factory=list)
    process: list[str] | None = Field(None, description="전형 절차")
    work_conditions: WorkConditions
    raw_meta: RawMeta
    preprocess_log: PreprocessLog
