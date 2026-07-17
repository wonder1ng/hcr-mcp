import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from hcr_mcp.config import Settings
from hcr_mcp.errors import HcrMcpError

logger = logging.getLogger("hcr_mcp.storage")

_NOTICE_MARKER = ".storage_notice_ack"
_STORAGE_NOTICE = (
    "안내: hcr-mcp는 처리 과정에서 생성되는 원문·분석 결과를 전부 로컬({data_dir})에 저장합니다"
    "(외부 서버로 전송되지 않음) — 같은 공고/이력서를 다시 분석할 때 재사용하기 위함입니다. "
    "이 안내는 최초 1회만 표시됩니다."
)


class Storage:
    def __init__(self, settings: Settings):
        self._data_dir = settings.data_dir

    def _kind_dir(self, kind: str) -> Path:
        d = self._data_dir / kind
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HcrMcpError(f"저장 폴더({d})를 만들 수 없습니다: {e}") from e
        return d

    def consume_first_use_notice(self) -> str | None:
        """이 서버가 로컬 저장을 시작하기 전 딱 한 번만 안내 메시지를 반환한다(마커 파일로
        추적, 이후 호출부터는 None). hcr-mcp는 저장 여부를 선택할 수 없고 항상 저장하므로
        (같은 채용공고를 다른 이력서로 재분석할 수 있어야 함) 매 호출 확인 대신 최초 1회
        고지로 대체 — install_system_certs의 '툴 호출 자체가 동의'와 다른 지점: 이쪽은
        저장이 기본 동작이라 사용자가 먼저 알 기회가 없으므로 이 안내가 그 역할을 한다."""
        marker = self._data_dir / _NOTICE_MARKER
        if marker.exists():
            return None
        try:
            marker.write_text("", encoding="utf-8")
        except OSError as e:
            logger.warning("최초 사용 안내 마커를 기록하지 못했습니다(다음 호출에 다시 표시될 수 있음): %s", e)
        return _STORAGE_NOTICE.format(data_dir=self._data_dir)

    def save_report(self, kind: str, key: str, result: dict[str, Any]) -> Path:
        """kind별 가공(분석) 결과를 data_dir/{kind}/{key}_{timestamp}.json 으로 항상 저장한다 —
        원시 데이터(save_raw)뿐 아니라 이 가공 결과도 재분석 시 재사용해야 하므로(예: 같은
        채용공고를 다른 이력서로 다시 분석) 저장 여부를 선택하게 하지 않는다."""
        now = datetime.now()
        result = {**result, "_saved_at": now.isoformat(timespec="seconds")}
        path = self._kind_dir(kind) / f"{key}_{now.strftime('%Y%m%dT%H%M%S')}.json"
        try:
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            raise HcrMcpError(
                f"결과를 저장하지 못했습니다({path}): {e}. 디스크 용량/쓰기 권한을 확인하세요."
            ) from e
        return path

    def save_raw(self, kind: str, key: str, filename: str, content: bytes) -> Path:
        """원시 중간 데이터(스크래핑 원문 등) 저장. 항상 저장한다 — 재수집 비용이 큰 데이터라
        재분석 시 재사용하기 위함(예: job_posting/collector.py, report_builder.py)."""
        raw_dir = self._kind_dir(kind) / key / "raw"
        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
            path = raw_dir / filename
            path.write_bytes(content)
        except OSError as e:
            raise HcrMcpError(
                f"원시 데이터를 저장하지 못했습니다({raw_dir}/{filename}): {e}. 디스크 용량/쓰기 권한을 확인하세요."
            ) from e
        return path

    def list_reports(self, kind: str, key_prefix: str | None = None) -> list[dict[str, Any]]:
        """최신순으로 정렬된 저장된 리포트 목록 (파일명 glob, DB 인덱스 없음).
        손상된 파일 하나 때문에 전체 목록 조회가 실패하지 않도록, 읽기 실패한 파일은 건너뛰고 로그만 남긴다.
        """
        pattern = f"{key_prefix}_*.json" if key_prefix else "*.json"
        paths = sorted(self._kind_dir(kind).glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        reports: list[dict[str, Any]] = []
        for p in paths:
            try:
                reports.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("저장된 리포트 파일을 읽지 못해 건너뜁니다: %s (%s)", p, e)
        return reports

    def latest_report(self, kind: str, key_prefix: str) -> dict[str, Any] | None:
        reports = self.list_reports(kind, key_prefix)
        return reports[0] if reports else None
