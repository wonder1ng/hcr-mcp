"""채용공고 구조화 스키마 (HcR/hiring_preprocess/job_schema_v2.json copy-adapt).

원본과의 차이:
- 배치용 추적 필드 제거: raw_meta.source_file/source_row(TSV 행 추적용, 단발 호출엔 의미 없음),
  preprocess_log.original_text_snapshot(LLM 캐시 대조용 — 원문은 storage.save_raw로 이미 통째로 보존).
- raw_meta.ocr_used → vision_used로 개명(EasyOCR이 아니라 llm_client.vision_extract 사용).
- raw_meta.llm_used 제거: 원본은 USE_LLM=0 스킵 모드·LLM 실패 시 empty_record() 폴백이 있어
  이 값을 구분할 이유가 있었지만, 이 collector는 그 두 경로가 없다(실패 시 예외로 전체 호출이
  실패, 부분 레코드를 만들어 저장하지 않음) — 저장된 레코드는 항상 LLM 성공을 의미해 이 필드가
  결과에 아무 정보도 더하지 않는다(실측: 항상 LLM이 임의로 채운 값만 나옴, 신뢰 불가).
- jobs: prompts.py에 "모집분야가 하나뿐이어도 리스트로 반환" + "내용이 전혀 없으면 빈 배열"
  두 규칙을 프롬프트 레벨로만 강제한다.
- 신규: jobs[].department — 공고에 언급된 팀/본부/사업부명을 원문 그대로 추출(부서→사업분야
  매핑은 안 함, industry_keyword.py 몫). 없으면 null.
- 신규: jobs[].career — 지원자격 섹션 등에 명시된 경력 요건 원문(예: "경력무관"). 원본
  job_schema_v2.json에는 없던 필드 — 실측 확인 결과 이 값이 tracks.newcomer.requirements 안에
  섞여 들어가 "신입 전용 요건"처럼 잘못 읽히는 문제가 있어(fit/schemas.py의 JobProfile.career와
  같은 이유로 별도 필드 필요) 최상위 자격요건 성격의 필드로 분리.
- documents → documents_required/documents_optional로 분리(JobCommon, Track 둘 다): 실측 확인
  결과 제출서류가 "공통필수"/"해당선택"으로 나뉘어 있는데 원본은 documents 하나뿐이라 필수 아닌
  서류가 통째로 드롭되는 문제가 있었음.
- 신규: jobs[].skills/core_competencies — 잡코리아 등 채용 사이트의 "모집요강" 사이드바에 우대
  사항 문장과 별개로 태그 형태로 나열되는 "스킬"/"핵심역량" 항목(예: 스킬: Ai, 머신러닝 / 핵심
  역량: 계획성, 성실성). 기존 preferred_common/tracks 어디에도 안 맞아 드롭되던 걸 확인해 추가.
- 신규: raw_text — 스크래핑/비전 추출 원문 전체. LLM이 채우는 게 아니라 collector.py가 LLM
  호출 이후 posting_text로 직접 덮어쓴다(raw_meta.source_url/vision_used와 동일한 패턴).
- 원본은 OpenAI Responses API strict 모드용이라 모든 필드가 필수(빈 값은 ""/[])였지만, 이
  프로젝트는 LangChain with_structured_output + Pydantic(fit/schemas.py와 동일 패턴)을 쓰므로
  없는 값은 진짜 None으로 표현한다.
"""

from pydantic import BaseModel, Field


class JobCommon(BaseModel):
    education: str | None = Field(None, description="공고 전체 공통 학력")
    major: str | None = Field(None, description="공고 전체 공통 전공")
    preferred: list[str] | None = Field(None, description="공고 전체 모든 직무에 적용되는 공통 우대사항만(특정 직무 우대사항은 여기 넣지 않음)")
    documents_required: list[str] | None = Field(None, description="공통 제출서류 중 필수(예: '공통필수')")
    documents_optional: list[str] | None = Field(None, description="공통 제출서류 중 해당자만/선택 제출(예: '해당선택')")


class Track(BaseModel):
    requirements: list[str] | None = Field(None, description="이 트랙(신입/경력) 전용 자격요건")
    preferred: list[str] | None = Field(None, description="이 트랙 전용 우대사항")
    responsibilities: list[str] | None = Field(None, description="이 트랙 전용 업무")
    documents_required: list[str] | None = Field(None, description="이 트랙 전용 제출서류 중 필수")
    documents_optional: list[str] | None = Field(None, description="이 트랙 전용 제출서류 중 해당자만/선택 제출")


class Tracks(BaseModel):
    newcomer: Track | None = Field(None, description="신입 전용 조건")
    experienced: Track | None = Field(None, description="경력 전용 조건")


class JobEntry(BaseModel):
    job_name: str = Field(description="모집분야/직무명")
    department: str | None = Field(None, description="공고에 팀·본부·사업부명이 명시돼 있으면 원문 그대로 추출. 추측 금지, 없으면 null")
    career: str | None = Field(
        None,
        description=(
            "지원자격에 명시된 경력 요건 원문(예: '경력무관', '신입 지원 가능', '경력 3년 이상'). "
            "신입/경력 트랙별 세부 자격요건·우대사항은 tracks에 넣고, 여기는 지원자격 섹션 등에 "
            "명시된 경력 조건 자체만 담는다(tracks.newcomer/experienced.requirements에 중복 기재 금지)"
        ),
    )
    headcount: str | None = Field(None, description="모집인원 원문 표현 그대로 보존(예: '0명'도 미기재가 아니라 원문 그대로)")
    education: str | None = Field(None, description="이 직무의 학력 요건(고졸/전문학사/학사/석사/박사/무관). 자격증·어학점수는 여기 넣지 않음")
    major: str | None = Field(None, description="이 직무의 전공 요건")
    locations: list[str] | None = Field(None, description="근무지")
    responsibilities: list[str] | None = Field(None, description="신입/경력 공통 실제 수행 업무만(자격요건 아님)")
    preferred_common: list[str] | None = Field(None, description="이 직무 신입/경력 모두에 적용되는 우대사항(트랙 전용 우대사항과 중복 금지)")
    skills: list[str] | None = Field(None, description="공고에 '스킬'/'기술' 등으로 명시된 기술·도구 태그 목록(우대사항 문장과 별개로 태그 형태로 나열된 것)")
    core_competencies: list[str] | None = Field(None, description="공고에 '핵심역량'/'인재상' 등으로 명시된 역량·가치 키워드 목록(예: 계획성, 성실성, 창의성)")
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
    jobs: list[JobEntry] = Field(
        description="모집분야별로 분리된 직무 목록. 모집분야가 하나뿐이어도 그 하나를 반드시 포함"
        "(원문에 파싱할 내용이 전혀 없을 때만 빈 배열)"
    )
    process: list[str] | None = Field(None, description="전형 절차")
    work_conditions: WorkConditions
    raw_meta: RawMeta
    preprocess_log: PreprocessLog
    raw_text: str | None = Field(None, description="스크래핑/비전 추출로 확보한 공고 원문 전체")
