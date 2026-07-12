"""HTTPS 요청 시 SSL 인증서 신뢰 문제 감지 + 사용자 동의 기반 수동 해결 지원.

회사망 프록시나 일부 백신의 TLS 검사 때문에 Python(certifi 번들)이 신뢰하지 못하는 루트
인증서가 중간에 끼는 경우가 있다(SSLCertVerificationError). 이건 "이 회사는 데이터 없음" 같은
콜렉터의 정상적인 부분 실패와 성격이 다르다 — 네트워크 전체가 막힌 시스템 오류라서 조용히
삼키면 안 되고, 원인과 해결책을 사용자에게 명확히 안내해야 한다.

해결책(pip-system-certs)은 인터프리터 시작 시점에 .pth 훅으로 ssl 모듈을 패치하는 방식이라
설치만으로는 이미 떠 있는 프로세스(hcr-mcp는 MCP 서버로 계속 살아있는 프로세스)에 반영되지
않는다 — 그래서 설치 후 hcr-mcp 재시작이 필수다. 자동으로 설치하지 않고 install_system_certs
툴을 사용자가 직접 호출해야 설치가 실행된다(설치는 사용자 환경을 바꾸는 행위라 명시적 동의가
필요하고, MCP 툴 호출 자체가 그 동의 지점이다).
"""

import ssl
import subprocess
import sys

from hcr_mcp.errors import HcrMcpError


class SslTrustError(HcrMcpError):
    """SSL 인증서 검증 실패 — 개별 요청의 일시적 실패가 아니라 환경 전체의 신뢰 체인 문제."""


def _is_cert_verify_error(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def raise_if_ssl_trust_error(exc: BaseException) -> None:
    """httpx 예외의 원인 체인에 SSL 인증서 검증 실패가 있으면 명확한 안내와 함께 즉시 raise한다.

    콜렉터의 `except httpx.HTTPError` 블록 맨 앞에서 호출한다. 해당되면 여기서 위로 전파하고,
    아니면 그냥 반환해 호출자가 기존 부분 실패 처리(조용히 건너뛰기 등)를 계속하게 한다.
    """
    if _is_cert_verify_error(exc):
        raise SslTrustError(
            "SSL 인증서 검증에 실패했습니다. 사내망 프록시나 일부 백신의 TLS 검사 때문에 "
            "이 PC가 신뢰하지 못하는 인증서가 중간에 끼어있을 가능성이 높습니다. "
            "install_system_certs 툴을 호출하면 pip-system-certs를 설치해 이 PC의 OS 인증서 "
            "저장소를 그대로 신뢰하도록 만들 수 있습니다(설치 후 hcr-mcp 재시작 필요)."
        ) from exc


def install_pip_system_certs() -> str:
    """pip-system-certs 설치. install_system_certs 툴에서만 호출한다 — 사용자가 그 툴을
    명시적으로 호출하는 것 자체가 환경 변경에 대한 동의다."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "pip-system-certs"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise HcrMcpError(f"pip-system-certs 설치에 실패했습니다: {e.stderr}") from e
    return (
        "pip-system-certs 설치 완료.\n" + result.stdout.strip() +
        "\n\n이 PC의 인증서 저장소가 적용되려면 hcr-mcp 프로세스를 재시작해야 합니다 "
        "(인터프리터 시작 시점에 적용되는 패치라 지금 이 프로세스에는 반영되지 않습니다)."
    )
