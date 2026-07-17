from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from hcr_mcp.errors import HcrMcpError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HCR_MCP_", env_file=".env", extra="ignore")

    llm_api_key: str  # OpenAI API 키 (채팅+임베딩 공용 기본값)
    llm_embedding_api_key: str | None = None  # 생략 시 llm_api_key 재사용. 임베딩만 별도 키/과금으로 분리하고 싶을 때만 설정.
    llm_chat_model: str = "gpt-4o-mini"
    llm_embedding_model: str = "text-embedding-3-small"

    dart_api_key: str | None = None

    data_dir: Path = Path.home() / ".hcr-mcp" / "data"


def load_settings() -> Settings:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as e:
        missing = ", ".join(f"HCR_MCP_{err['loc'][0]}".upper() for err in e.errors() if err["type"] == "missing")
        detail = f" 누락된 값: {missing}." if missing else f" {e}"
        raise HcrMcpError(
            "hcr-mcp 설정을 읽지 못했습니다."
            + detail
            + " MCP 클라이언트 설정(예: Claude Desktop config)의 env 항목에 값을 추가하거나 .env 파일을 만드세요."
        ) from e

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HcrMcpError(
            f"데이터 저장 경로({settings.data_dir})를 만들 수 없습니다: {e}. "
            "HCR_MCP_DATA_DIR 환경변수로 쓰기 가능한 다른 경로를 지정하세요."
        ) from e

    return settings
