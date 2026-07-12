import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from hcr_mcp.config import Settings, StorageLevel
from hcr_mcp.errors import HcrMcpError

logger = logging.getLogger("hcr_mcp.storage")


class Storage:
    def __init__(self, settings: Settings):
        self._data_dir = settings.data_dir
        self._default_level = settings.default_storage_level

    def _kind_dir(self, kind: str) -> Path:
        d = self._data_dir / kind
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HcrMcpError(f"저장 폴더({d})를 만들 수 없습니다: {e}") from e
        return d

    def save_report(
        self,
        kind: str,
        key: str,
        result: dict[str, Any],
        storage_level: StorageLevel | None = None,
    ) -> Path | None:
        """kind별 결과를 data_dir/{kind}/{key}_{timestamp}.json 으로 저장.
        storage_level == "none" 이면 저장하지 않고 None 반환 (결과는 호출자가 그대로 리턴).
        """
        level = storage_level or self._default_level
        if level == "none":
            return None
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
        """storage_level == 'raw' 일 때만 호출하는 원시 중간 데이터 저장."""
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
