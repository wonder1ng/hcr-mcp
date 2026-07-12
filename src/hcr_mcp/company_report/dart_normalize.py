"""DART 원본 응답(직원현황·재무지표) 정규화 (HcR/dartAPI/normalize.py에서 순수 함수만 그대로 발췌).

재무제표(normalize_finances)는 제외 — dart_collector.py는 더 단순한 fnlttSinglAcnt.json(주요계정)을
쓰기 때문에 fnlttSinglAcntAll.json 전용으로 설계된 그 정규화기가 필요 없다.
"""


def _to_int(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _to_float(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _employee_metrics(row: dict) -> dict:
    return {
        "head_count": _to_int(row.get("sm")),
        "regular": _to_int(row.get("rgllbr_co")),
        "contract": _to_int(row.get("cnttk_co")),
        "avg_tenure": _to_float(row.get("avrg_cnwk_sdytrn")),
        "avg_salary": _to_int(row.get("jan_salary_am")),
        "total_salary": _to_int(row.get("fyer_salary_totamt")),
    }


_SEX_MAP: dict[str, str] = {"남": "male", "여": "female"}
_TOTAL_LABELS: set[str] = {"전사", "성별합계", "합계"}


def _norm_division(fo_bbm: str | None) -> str:
    label = (fo_bbm or "").strip()
    if label in _TOTAL_LABELS or label in ("", "-"):
        return "total"
    return label


def _norm_sex(sexdstn: str | None) -> str:
    label = (sexdstn or "전체").strip()
    return _SEX_MAP.get(label, label)


def normalize_employees(rows: list[dict]) -> list[dict]:
    """직원현황 원본 행들을 (회사·연도) 단위 레코드로 묶는다."""
    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []

    for row in rows:
        key = (row.get("corp_code"), row.get("bsns_year"))
        if key not in grouped:
            grouped[key] = {
                "corp_name": row.get("corp_name"),
                "corp_code": row.get("corp_code"),
                "bsns_year": row.get("bsns_year"),
                "divisions": {},
            }
            order.append(key)
        division = _norm_division(row.get("fo_bbm"))
        sex = _norm_sex(row.get("sexdstn"))
        grouped[key]["divisions"].setdefault(division, {})[sex] = _employee_metrics(row)

    return [grouped[k] for k in order]


def normalize_indicators(rows: list[dict]) -> list[dict]:
    """재무지표 원본 행들을 (회사·연도) 단위 레코드로 묶는다.

    구조: 지표분류(idx_cl_nm: 수익성/안정성/성장성/활동성지표) → 지표명(idx_nm) → 값.
    """
    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []

    for row in rows:
        key = (row.get("corp_code"), row.get("bsns_year"))
        if key not in grouped:
            grouped[key] = {
                "corp_name": row.get("corp_name"),
                "corp_code": row.get("corp_code"),
                "bsns_year": row.get("bsns_year"),
                "indicators": {},
            }
            order.append(key)
        category = row.get("idx_cl_nm") or "기타"
        name = row.get("idx_nm")
        if name:
            grouped[key]["indicators"].setdefault(category, {})[name] = _to_float(row.get("idx_val"))

    return [grouped[k] for k in order]
