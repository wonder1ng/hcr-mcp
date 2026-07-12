"""회사 분석 보고서 base 스키마 — 사용자가 제공한 hcr-backend company_analyses 샘플을
결과 형태(필드명·구조) 참고용으로 삼아 새로 설계. source_keys는 원본에서도 항상 빈 배열이라
드롭(참조할 별도 sources 테이블이 v1엔 없음)."""

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    text: str
    evidence_type: str = Field(description="'dart' | 'profile' | 'inference' — 이 근거가 어디서 왔는지")


class SummaryBlock(BaseModel):
    summary: str
    evidence: list[Evidence]


class CompanyReportBase(BaseModel):
    """fresh_generator.py의 LLM 합성 출력. report_builder.py가 여기에 company_name/
    source_snapshot/yearly_issues 등 비-LLM 필드를 덧붙여 최종 report.json을 만든다."""

    industry_status: str | None = Field(None, description="업종·산업 내 위치 한두 문장. 근거 없으면 null")
    financial_analysis: SummaryBlock
    growth_potential: SummaryBlock
    swot_strengths: list[Evidence]
    swot_weaknesses: list[Evidence]
    swot_opportunities: list[Evidence]
    swot_threats: list[Evidence]
    key_points: list[Evidence]
