"""이력서 파싱 스키마 (hcr-backend/app/documents/schemas.py의 이력서 분기만 발췌)."""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """지정되지 않은 임의의 필드 유입을 방지하는 엄격한 베이스 모델"""

    model_config = ConfigDict(extra="forbid")


class University(StrictModel):
    name: Optional[str] = None
    score: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    location: Optional[str] = None
    major: Optional[str] = None
    graduate: Optional[str] = None


class Career(StrictModel):
    name: Optional[str] = None
    department: Optional[str] = None
    position: Optional[str] = None
    responsibilities: Optional[str] = None
    leaving_reason: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class Certification(StrictModel):
    name: Optional[str] = None
    organization: Optional[str] = None
    date: Optional[str] = None


class Award(StrictModel):
    date: Optional[str] = None
    name: Optional[str] = None
    organization: Optional[str] = None
    description: Optional[str] = None


class Education(StrictModel):
    name: Optional[str] = None
    organization: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    description: Optional[str] = None


class ToolSkill(StrictModel):
    name: Optional[str] = None
    proficiency: Optional[str] = None


class Resume(StrictModel):
    """이력서 전체 구조를 취합하는 검증 스키마"""

    school: List[University] = None
    career: List[Career] = None
    certifications: List[Certification] = None
    awards: List[Award] = None
    education: List[Education] = None
    tools_skills: List[ToolSkill] = None
    created_datetime: Optional[str] = None


class ResumeRoute(BaseModel):
    """이력서 체인 전용 최종 통합 구조 분기 래퍼"""

    response_type: Literal["success", "fail"] = Field(
        ..., description="이력서가 정상 추출되면 'success', 구직 서류와 무관한 잘못된 문서라면 'fail'로 지정하세요."
    )
    resume: Optional[Resume] = Field(None, description="성공적으로 빌드된 이력서 본문 데이터 (response_type이 'success'일 때만 채움)")
    reason: Optional[str] = Field(None, description="문서를 생성하거나 처리할 수 없는 구체적인 이유 설명 (response_type이 'fail'일 때만 채움)")
    suggestion: Optional[str] = Field(None, description="사용자가 올바른 요청을 할 수 있도록 돕는 유도 가이드라인 문구 (response_type이 'fail'일 때만 채움)")
