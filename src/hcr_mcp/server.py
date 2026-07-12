import sys

from mcp.server.fastmcp import FastMCP

from hcr_mcp import llm_client, net
from hcr_mcp.config import Settings, load_settings
from hcr_mcp.errors import HcrMcpError
from hcr_mcp.storage import Storage

mcp = FastMCP("hcr-mcp")

_storage: Storage | None = None


def get_storage() -> Storage:
    if _storage is None:
        raise HcrMcpError("내부 오류: 서버가 아직 초기화되지 않았습니다.")
    return _storage


@mcp.tool()
def install_system_certs() -> str:
    """SSL 인증서 검증 실패(SslTrustError) 안내를 받았을 때 호출하는 복구 툴.

    pip-system-certs를 설치해 이 PC의 OS 인증서 저장소(회사망 프록시·백신이 심은 루트 인증서
    포함)를 그대로 신뢰하도록 만든다. 설치 후에는 hcr-mcp 프로세스를 재시작해야 적용된다.
    """
    return net.install_pip_system_certs()


def _init(settings: Settings) -> None:
    global _storage
    llm_client.init_llm_client(settings)
    _storage = Storage(settings)


def main() -> None:
    try:
        settings = load_settings()
    except HcrMcpError as e:
        print(f"[hcr-mcp] 시작 실패: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    _init(settings)
    mcp.run()


if __name__ == "__main__":
    main()
