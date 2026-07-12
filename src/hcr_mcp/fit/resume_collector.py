"""이력서 파일(PDF) → 구조화 텍스트 (hcr-backend/app/documents/service.py 이력서 분기만 copy-adapt).

Mongo 저장 없음, UploadFile 대신 로컬 파일 경로, BYOK 클라이언트 사용.
"""

from datetime import datetime
from pathlib import Path

import fitz
from langchain_core.prompts import ChatPromptTemplate

from hcr_mcp import llm_client
from hcr_mcp.errors import HcrMcpError
from hcr_mcp.fit.resume_schemas import ResumeRoute

_BASE_RULE = """
1. PDF에서 추출된 텍스트를 분석한다.
2. 오타 자동 수정
3. 띄어쓰기 자동 수정
4. 날짜 형식 통일

YYYY-MM-DD
YYYY-MM
YYYY

5. 없는 값은 null
6. 추론 금지
7. 중복 제거
8. 모든 정보를 구조화
9. 최대한 상세하게 추출."""

# 이력서와 무관한 문서(뉴스·잡담 등)가 들어왔을 때 LLM이 억지로 데이터를 채우지 않고
# 명확한 실패 사유를 반환하게 하는 가드레일 — 안전 필터가 아니라 환각 방지용 데이터 품질 규칙.
_EXCEPTION_RULE = """
[중요 - 부적절한 문서 필터링 규칙]
1. 분석할 문서 본문이 비어있거나, 이력서와 전혀 무관한 텍스트(예: 상식 질문, 뉴스, 잡담, 코딩 등)인 경우 절대 데이터를 채우지 마십시오.
2. 위와 같이 부적절한 문서가 유입되면, 무조건 response_type을 'fail'로 지정하고 그 이유와 해결 방법을 반환해야 합니다."""

_SYSTEM_INSTRUCTION = f"""\
당신은 이력서(Resume)를 전문 분석하는 ATS 이력서 파서입니다.
규칙
{_BASE_RULE}
{_EXCEPTION_RULE}
"""


def _extract_text_from_pdf(path: Path) -> str:
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise HcrMcpError(f"이력서 PDF를 열지 못했습니다({path}): {e}. 파일이 손상되지 않았는지 확인하세요.") from e
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


async def parse_resume(path: str | Path) -> dict:
    """이력서 PDF 경로를 받아 구조화된 이력서 dict를 반환.

    실패(이력서와 무관한 문서 등) 시 HcrMcpError로 원인과 해결 방법을 전달한다.
    """
    path = Path(path)
    if not path.exists():
        raise HcrMcpError(f"이력서 파일을 찾을 수 없습니다: {path}")

    text = _extract_text_from_pdf(path)
    if not text.strip():
        raise HcrMcpError(f"이력서 PDF({path})에서 텍스트를 추출하지 못했습니다. 스캔 이미지형 PDF는 지원하지 않습니다.")

    structured_llm = llm_client.get_chat_model().with_structured_output(ResumeRoute)
    prompt = ChatPromptTemplate.from_messages(
        [("system", _SYSTEM_INSTRUCTION), ("human", "[분석할 문서 본문]\n{text}")]
    )
    result: ResumeRoute = await (prompt | structured_llm).ainvoke({"text": text})

    if result.response_type == "fail":
        raise HcrMcpError(
            f"이력서로 인식할 수 없는 문서입니다: {result.reason or '알 수 없는 사유'}. "
            f"{result.suggestion or ''}"
        )

    final = (result.resume.model_dump() if result.resume else {})
    final["created_datetime"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return final