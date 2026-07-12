"""DART Open API 라이브 조회 (HcR/dartAPI/corp_code.py + dart_api.py copy-adapt).

원본 차이점: `HcR/dartAPI/config.py`가 모듈 임포트 시점에 `DART_API_KEY`를 환경변수에서 읽어
없으면 즉시 `raise ValueError`한다 — 이 모듈은 그 대신 `crtfc_key`를 모든 함수에 명시적
파라미터로 받는다.

호출 규칙: DART 키가 설정되어 있는지는 이 모듈이 판단하지 않는다 — 호출자(report_builder.py)가
`if settings.dart_api_key:` 조건문으로 먼저 걸러서, 키가 없으면 이 모듈을 아예 호출하지 않는다
(재무/인력 섹션 스킵). 여기서 발생하는 예외는 "키를 넣었는데 실제로 잘못됐다/API가 실패했다" 같은
진짜 오류만 다룬다 — "키가 없음"은 애초에 이 모듈에 도달하지 않는 정상적인 분기 문제이지 오류가 아니다.

corp_code 목록(전체 상장·비상장사)은 크기가 있어(수만 건) 로컬에 캐시한다(HcR 원본과 동일).
"""

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path

import httpx

from hcr_mcp import net
from hcr_mcp.company_report import dart_normalize
from hcr_mcp.errors import HcrMcpError

BASE_URL = "https://opendart.fss.or.kr/api"
CORP_CODE_URL = f"{BASE_URL}/corpCode.xml"

_STRIP_RE = re.compile(r"주식회사|㈜|\(주\)|\(유\)|유한회사")
_IDX_CL_CODES = ("M210000", "M220000", "M230000", "M240000")  # 수익성/안정성/성장성/활동성


def _normalize_name(name: str) -> str:
    name = _STRIP_RE.sub("", name)
    return re.sub(r"[\s().,&]", "", name).strip()


async def _get(client: httpx.AsyncClient, crtfc_key: str, endpoint: str, params: dict) -> dict | None:
    try:
        resp = await client.get(f"{BASE_URL}/{endpoint}", params={**params, "crtfc_key": crtfc_key}, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        net.raise_if_ssl_trust_error(e)
        raise HcrMcpError(f"DART API 호출에 실패했습니다({endpoint}): {e}") from e

    data = resp.json()
    if data.get("status") == "013":
        return None  # 조회된 데이터 없음 — 정상적으로 발생 가능
    if data.get("status") == "010":
        raise HcrMcpError("DART API 키가 유효하지 않습니다. HCR_MCP_DART_API_KEY 값을 확인하세요.")
    if data.get("status") != "000":
        raise HcrMcpError(f"DART API 오류(status={data.get('status')}): {data.get('message')}")
    return data


def _pick_best(candidates: list[dict]) -> str | None:
    if not candidates:
        return None
    listed = [r for r in candidates if r.get("stock_code")]
    pool = listed or candidates
    return max(pool, key=lambda r: r.get("modify_date", ""))["corp_code"]


async def _load_corp_codes(client: httpx.AsyncClient, crtfc_key: str, cache_path: Path) -> list[dict]:
    if cache_path.exists():
        with cache_path.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    try:
        resp = await client.get(CORP_CODE_URL, params={"crtfc_key": crtfc_key}, timeout=30)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_data = zf.read("CORPCODE.xml")
    except (httpx.HTTPError, zipfile.BadZipFile) as e:
        if isinstance(e, httpx.HTTPError):
            net.raise_if_ssl_trust_error(e)
        raise HcrMcpError(f"DART 기업 고유번호 목록을 내려받지 못했습니다: {e}") from e

    root = ET.fromstring(xml_data)
    records = [
        {
            "corp_code": item.findtext("corp_code", "").strip(),
            "corp_name": item.findtext("corp_name", "").strip(),
            "stock_code": item.findtext("stock_code", "").strip(),
            "modify_date": item.findtext("modify_date", "").strip(),
        }
        for item in root.findall("list")
    ]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["corp_code", "corp_name", "stock_code", "modify_date"])
        writer.writeheader()
        writer.writerows(records)
    return records


async def get_corp_code(client: httpx.AsyncClient, crtfc_key: str, corp_name: str, cache_path: Path) -> str | None:
    """3단계 매칭(HcR/dartAPI/corp_code.py::get_corp_code 그대로): 정확일치 → 정규화일치 → 정규화 포함관계."""
    records = await _load_corp_codes(client, crtfc_key, cache_path)

    exact = [r for r in records if r["corp_name"] == corp_name]
    if exact:
        return _pick_best(exact)

    norm_input = _normalize_name(corp_name)
    norm_exact = [r for r in records if _normalize_name(r["corp_name"]) == norm_input]
    if norm_exact:
        return _pick_best(norm_exact)

    if len(norm_input) >= 4:
        candidates = [r for r in records if norm_input in _normalize_name(r["corp_name"])]
        if candidates:
            min_len = min(len(_normalize_name(r["corp_name"])) for r in candidates)
            shortest = [r for r in candidates if len(_normalize_name(r["corp_name"])) == min_len]
            return _pick_best(shortest)

    return None


async def collect_dart_data(crtfc_key: str, corp_name: str, cache_dir: Path) -> dict | None:
    """회사명 → 최근 확보 가능한 연도의 주요 재무계정 + 재무지표 + 직원현황.

    crtfc_key가 설정되어 있다는 전제로 호출된다(호출자가 조건문으로 이미 걸러냄).
    corp_code를 못 찾거나 최근 2개년 모두 데이터가 없으면(신생/비공시 기업 등) None.
    """
    async with httpx.AsyncClient() as client:
        corp_code = await get_corp_code(client, crtfc_key, corp_name, cache_dir / "dart_corp_codes.csv")
        if not corp_code:
            return None

        this_year = date.today().year
        finance = None
        bsns_year = None
        for candidate_year in (str(this_year - 1), str(this_year - 2)):
            finance = await _get(
                client, crtfc_key, "fnlttSinglAcnt.json",
                {"corp_code": corp_code, "bsns_year": candidate_year, "reprt_code": "11011"},
            )
            if finance:
                bsns_year = candidate_year
                break

        if not bsns_year:
            return {"corp_code": corp_code, "bsns_year": None, "finance": None, "employees": None, "indicators": None}

        employees_raw = await _get(
            client, crtfc_key, "empSttus.json",
            {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": "11011"},
        )

        indicator_rows: list[dict] = []
        for idx_cl_code in _IDX_CL_CODES:
            resp = await _get(
                client, crtfc_key, "fnlttSinglIndx.json",
                {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": "11011", "idx_cl_code": idx_cl_code},
            )
            if resp and resp.get("list"):
                indicator_rows.extend(resp["list"])

    employees = dart_normalize.normalize_employees(employees_raw["list"])[0] if employees_raw and employees_raw.get("list") else None
    indicators = dart_normalize.normalize_indicators(indicator_rows)[0]["indicators"] if indicator_rows else None

    return {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "finance": finance.get("list") if finance else None,
        "employees": employees,
        "indicators": indicators,
    }
