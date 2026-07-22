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

import asyncio
import csv
import io
import re
import warnings
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

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
            "corp_eng_name": item.findtext("corp_eng_name", "").strip(),
            "stock_code": item.findtext("stock_code", "").strip(),
            "modify_date": item.findtext("modify_date", "").strip(),
        }
        for item in root.findall("list")
    ]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["corp_code", "corp_name", "corp_eng_name", "stock_code", "modify_date"])
        writer.writeheader()
        writer.writerows(records)
    return records


def _matches(record: dict, norm_name: str) -> bool:
    """정규화한 이름이 이 회사의 국문명(corp_name) 또는 영문명(corp_eng_name) 중 하나와
    일치하는지 — DART 등록명이 채용공고 표기와 다를 수 있어(구 상호·영문 표기 등)
    두 필드 모두 대조 대상으로 삼는다."""
    return norm_name in (_normalize_name(record["corp_name"]), _normalize_name(record.get("corp_eng_name") or ""))


def _contains(record: dict, norm_name: str) -> bool:
    return norm_name in _normalize_name(record["corp_name"]) or norm_name in _normalize_name(record.get("corp_eng_name") or "")


def _find_candidates(records: list[dict], names_to_try: list[str]) -> tuple[list[dict], str]:
    """3단계 매칭(정확일치 → 정규화일치 → 정규화 포함관계)을 corp_name과 corp_eng_name 두 필드
    모두에 대해, names_to_try(corp_name + 이미 확보한 다른 표기)의 각 이름으로 순서대로 시도한다.
    처음으로 결과가 나온 단계의 후보 목록 전체와 그 단계 이름("exact"|"normalized"|"fuzzy")을
    반환한다(단일 채택은 호출자가 get_corp_code에서 결정 — 애매한 단계는 대표자명/사업자번호
    검증이 필요하므로 여기서 하나로 좁히지 않는다). 아무 것도 못 찾으면 ([], "").

    채용공고 표기가 DART 등록명(정식 국문명 또는 영문명)과 달라도 이 중 하나만 맞으면 찾을 수
    있게 하기 위함 — DART API 자체엔 이름 외 필드(주소·대표자 등)로 역검색하는 기능이 없어,
    여기서 시도 가능한 건 이미 알고 있는 이름 후보들을 넓게 대조하는 것까지다. 네트워크 호출이
    없는 순수 함수라 get_corp_code에서 분리(오프라인 검증은 tests/verify_dart_corp_matching.py)."""
    for name in names_to_try:
        exact = [r for r in records if name in (r["corp_name"], r.get("corp_eng_name"))]
        if exact:
            return exact, "exact"

    for name in names_to_try:
        norm_input = _normalize_name(name)
        norm_exact = [r for r in records if _matches(r, norm_input)]
        if norm_exact:
            return norm_exact, "normalized"

    for name in names_to_try:
        norm_input = _normalize_name(name)
        if len(norm_input) < 4:
            continue
        candidates = [r for r in records if _contains(r, norm_input)]
        if candidates:
            min_len = min(len(_normalize_name(r["corp_name"])) for r in candidates)
            return [r for r in candidates if len(_normalize_name(r["corp_name"])) == min_len], "fuzzy"

    return [], ""


def _rank_candidates(candidates: list[dict]) -> list[dict]:
    """상장(주식코드 보유) 우선, 동순위 내에서는 최근 수정일 우선 — 검증 호출 시도 순서
    (기업개황 API를 후보 전부에 걸지 않고 유력한 순서대로 하나씩 확인하기 위함)."""
    by_recency = sorted(candidates, key=lambda r: r.get("modify_date", ""), reverse=True)
    return sorted(by_recency, key=lambda r: r.get("stock_code") == "")


_BIZ_NO_STRIP_RE = re.compile(r"[^0-9]")
_CEO_TITLE_RE = re.compile(r"대표이사|대표|회장|사장|CEO", re.IGNORECASE)


def _normalize_biz_no(no: str | None) -> str:
    return _BIZ_NO_STRIP_RE.sub("", no or "")


def _same_biz_no(a: str | None, b: str | None) -> bool:
    na, nb = _normalize_biz_no(a), _normalize_biz_no(b)
    return bool(na) and na == nb


def _normalize_person_name(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"\s+", "", _CEO_TITLE_RE.sub("", name)).strip()


def _same_person(a: str | None, b: str | None) -> bool:
    na, nb = _normalize_person_name(a), _normalize_person_name(b)
    return bool(na) and na == nb


def _is_verified_match(overview: dict, known_biz_no: str | None, known_ceo_name: str | None) -> bool:
    """사업자등록번호가 있으면 그것만으로 판단한다(국세청에 등록된 유일 식별자라 이름보다
    신뢰도가 높음). 없으면 대표자명으로 대체 판단한다(대표자 교체 가능성이 있어 차선책)."""
    if known_biz_no:
        return _same_biz_no(overview.get("bizr_no"), known_biz_no)
    if known_ceo_name:
        return _same_person(overview.get("ceo_nm"), known_ceo_name)
    return False


_MAX_VERIFY_CANDIDATES = 5  # ponytail: 애매한 후보가 이보다 많으면 검증 호출을 여기서 멈춘다(흔치 않은 경우)


async def _verify_candidates(
    client: httpx.AsyncClient, crtfc_key: str, candidates: list[dict], known_biz_no: str | None, known_ceo_name: str | None
) -> str | None:
    """애매한 후보(부분일치 단계이거나 후보 2개 이상)를 기업개황(company.json)으로 하나씩
    확인한다. 아무 후보도 확인되지 않으면 잘못된 회사 데이터를 붙이지 않도록 None을 반환한다
    (정보 없음이 부정확한 정보보다 낫다는 원칙)."""
    for r in _rank_candidates(candidates)[:_MAX_VERIFY_CANDIDATES]:
        overview = await _get(client, crtfc_key, "company.json", {"corp_code": r["corp_code"]})
        if overview and _is_verified_match(overview, known_biz_no, known_ceo_name):
            return r["corp_code"]
    return None


async def get_corp_code(
    client: httpx.AsyncClient,
    crtfc_key: str,
    corp_name: str,
    cache_path: Path,
    alt_names: list[str] | None = None,
    known_biz_no: str | None = None,
    known_ceo_name: str | None = None,
) -> str | None:
    """known_biz_no/known_ceo_name(홈페이지 크롤링 등으로 이미 확보한 사업자등록번호·대표자명)이
    있으면, 이름만으로는 후보가 여럿이거나(부분일치) 애매한 경우 기업개황 API로 실제 동일
    회사인지 확인한 뒤에만 채택한다. 정확일치 단계에서 후보가 하나뿐이면 검증 없이 그대로
    채택한다(대표자 교체 등으로 DART 데이터가 최신이 아닐 수 있어, 이미 이름이 확실한 경우까지
    거부하면 오히려 정상 매칭을 놓칠 위험이 커짐)."""
    records = await _load_corp_codes(client, crtfc_key, cache_path)
    candidates, tier = _find_candidates(records, [corp_name, *(alt_names or [])])
    if not candidates:
        return None

    needs_verification = tier == "fuzzy" or len(candidates) > 1
    if needs_verification and (known_biz_no or known_ceo_name):
        return await _verify_candidates(client, crtfc_key, candidates, known_biz_no, known_ceo_name)
    return _pick_best(candidates)


_QUARTER_MONTH_TO_REPRT_CODE = {"03": "11013", "09": "11014"}  # 분기보고서는 1/3분기 구분 필요


def _infer_reprt_code(report_nm: str) -> str | None:
    """정기보고서 report_nm(예: "사업보고서 (2024.12)")에서 재무정보 API가 받는 reprt_code를
    추론한다. 분기보고서는 1분기(11013)/3분기(11014) 둘 중 하나라 report_nm의 참조월로
    구분해야 한다."""
    if "사업보고서" in report_nm:
        return "11011"
    if "반기보고서" in report_nm:
        return "11012"
    if "분기보고서" in report_nm:
        m = re.search(r"\.(\d{2})\)", report_nm)
        return _QUARTER_MONTH_TO_REPRT_CODE.get(m.group(1)) if m else None
    return None


def _extract_bsns_year(report_nm: str) -> str | None:
    m = re.search(r"\((\d{4})\.\d{2}\)", report_nm)
    return m.group(1) if m else None


async def _latest_disclosure(
    client: httpx.AsyncClient, crtfc_key: str, corp_code: str, pblntf_ty: str, pblntf_detail_ty: str, bgn_de: str
) -> dict | None:
    """공시검색(list.json)으로 감사보고서(F001/F002)의 가장 최근 제출 건 하나를 찾는다
    (last_reprt_at=Y로 정정 이전 버전 제외, 그래도 여러 건 있을 수 있어 rcept_dt 최댓값을
    직접 고른다). F 타입은 pblntf_detail_ty가 실측상 정확히 걸러진다 — 반면 A 타입(정기보고서)은
    pblntf_detail_ty를 넣어도 사업/반기/분기보고서가 다 섞여 나와(2026-07-22 확인)
    _find_latest_periodic_reports가 report_nm 텍스트로 따로 분류한다."""
    data = await _get(
        client, crtfc_key, "list.json",
        {
            "corp_code": corp_code, "pblntf_ty": pblntf_ty, "pblntf_detail_ty": pblntf_detail_ty,
            "bgn_de": bgn_de, "last_reprt_at": "Y", "page_count": "10",
        },
    )
    rows = data.get("list") if data else None
    return max(rows, key=lambda r: r.get("rcept_dt", "")) if rows else None


async def _find_latest_periodic_reports(client: httpx.AsyncClient, crtfc_key: str, corp_code: str, bgn_de: str) -> dict[str, dict]:
    """공시검색(list.json)에서 정기보고서(사업/반기/분기보고서)를 한 번에 받아 report_nm
    텍스트로 분류한다. 반환: {"사업보고서": row, "반기보고서": row, "분기보고서": row} 중
    실제 존재하는 유형만, 각 유형의 가장 최근 제출 건."""
    data = await _get(
        client, crtfc_key, "list.json",
        {"corp_code": corp_code, "pblntf_ty": "A", "bgn_de": bgn_de, "last_reprt_at": "Y", "page_count": "100"},
    )
    latest: dict[str, dict] = {}
    for row in (data.get("list") if data else None) or []:
        report_nm = row.get("report_nm", "")
        for label in ("사업보고서", "반기보고서", "분기보고서"):
            if label in report_nm and (label not in latest or row["rcept_dt"] > latest[label]["rcept_dt"]):
                latest[label] = row
                break
    return latest


def _disclosure_meta(disclosure: dict | None) -> dict | None:
    return {"report_nm": disclosure["report_nm"], "rcept_dt": disclosure["rcept_dt"]} if disclosure else None


async def _fetch_full_financials(
    client: httpx.AsyncClient, crtfc_key: str, corp_code: str, bsns_year: str, reprt_code: str
) -> dict | None:
    """(사업/반기/분기보고서용) 전체 재무제표(fnlttSinglAcntAll.json)를 개별(OFS)+연결(CFS)
    둘 다 시도해 있는 것만 재무제표구분(sj_div)별로 그룹핑해 반환한다. LLM에게 그대로 넘길
    데이터라 계정과목명 등 회사마다 다를 수 있는 표기는 정규화하지 않고 원본 그대로 둔다."""
    result: dict[str, dict] = {}
    for fs_div in ("OFS", "CFS"):
        data = await _get(
            client, crtfc_key, "fnlttSinglAcntAll.json",
            {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code, "fs_div": fs_div},
        )
        rows = data.get("list") if data else None
        if not rows:
            continue
        sections: dict[str, dict] = {}
        for row in rows:
            section = sections.setdefault(row["sj_div"], {"name": row.get("sj_nm"), "accounts": {}})
            section["accounts"][row["account_nm"]] = {
                k: v for k, v in row.items()
                if k not in {"rcept_no", "reprt_code", "bsns_year", "corp_code", "sj_div", "sj_nm", "account_nm", "fs_div", "fs_nm"}
            }
        result[fs_div] = sections
    return result or None


_STATEMENT_LABELS = {"PBS": "재무상태표", "PIS": "손익계산서", "PEF": "자본변동표", "PCF": "현금흐름표"}
_PERIOD_LABELS = ("당기", "전기", "전전기")


def _parse_extraction_summary(soup: BeautifulSoup) -> dict:
    return {tag.get("acode"): tag.get_text(strip=True) for tag in soup.find_all("extraction") if tag.get("acode")}


def _parse_statement_table(table_group) -> dict[str, dict]:
    """감사보고서 원문 XML의 표 하나(재무상태표 등)를 {계정명: {"당기": ..., "전기": ...}}로
    변환한다. 계정과목명은 회사마다 다르지만 셀 구조(TE의 adelim 속성)는 DART 공통 스키마라
    범용 처리 가능 — 다만 당기/전기 값이 항상 같은 adelim 번호에 있지는 않고(대손충당금 같은
    차감 계정은 한 칸씩 밀려서 나옴, 실측 확인) 값이 있는 셀을 adelim 오름차순으로 나열해
    순서대로 당기→전기→전전기로 대응시킨다."""
    rows: dict[str, dict] = {}
    for tr in table_group.find_all("tr"):
        tes = tr.find_all("te")
        if not tes:
            continue
        label = None
        values = []
        for te in sorted(tes, key=lambda t: int(t.get("adelim") or 0)):
            text = te.get_text(strip=True)
            if te.get("adelim") == "0":
                label = text
            elif text:
                values.append(text)
        if label and values:
            rows[label] = dict(zip(_PERIOD_LABELS, values))
    return rows


async def _fetch_audit_report_financials(client: httpx.AsyncClient, crtfc_key: str, rcept_no: str) -> dict | None:
    """감사보고서 원문(document.xml — DART 자체 XML 포맷 1개 파일이 담긴 ZIP, 2026-07-22
    실측 확인)에서 요약 수치(SUMMARY/EXTRACTION)와 재무상태표/손익계산서/자본변동표/
    현금흐름표를 파싱한다. 사업보고서 제출 의무가 없어 fnlttSinglAcntAll.json 등 정기보고서
    전용 API로는 재무 수치를 못 가져오는 회사(감사보고서만 단독 제출)를 위한 유일한 구조화
    데이터 경로다."""
    resp = await client.get(
        "https://opendart.fss.or.kr/api/document.xml", params={"crtfc_key": crtfc_key, "rcept_no": rcept_no}, timeout=30
    )
    try:
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_text = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    except (httpx.HTTPError, zipfile.BadZipFile) as e:
        if isinstance(e, httpx.HTTPError):
            net.raise_if_ssl_trust_error(e)
        return None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(xml_text, "html.parser")

    summary = _parse_extraction_summary(soup)
    statements: dict[str, dict] = {}
    for group in soup.find_all("table-group"):
        aclass = group.get("aclass") or ""
        label = next((v for k, v in _STATEMENT_LABELS.items() if aclass.startswith(k)), None)
        if not label:
            continue
        rows = _parse_statement_table(group)
        if rows:
            statements[label] = rows

    return {"summary": summary, "statements": statements} if (summary or statements) else None


async def collect_dart_data(
    crtfc_key: str,
    corp_name: str,
    cache_dir: Path,
    alt_names: list[str] | None = None,
    known_biz_no: str | None = None,
    known_ceo_name: str | None = None,
) -> dict | None:
    """회사명 → 재무 데이터.

    감사보고서(F001/F002, 단독 제출 공시) 존재 여부를 먼저 확인해 있으면 그 원문에서 재무제표를
    파싱해서 쓰고, 없을 때만 (사업보고서 또는 반기보고서 중 더 최신인 쪽 — 이 둘은 내용 구성이
    동일함) + (최신 분기보고서 — 분기보고서는 내용이 상대적으로 적지만 3개월 더 최신)를
    fnlttSinglAcntAll.json으로 조회한다. 감사보고서와 사업/분기/반기보고서를 동시에 조회하지
    않는 이유(2026-07-22 실측 확인): 사업보고서 제출 의무가 있는 회사(예: 삼성전자)는 감사보고서가
    사업보고서의 첨부서류로 들어가 독립 공시(F001)로 안 잡히고(실측: status 013 데이터없음),
    감사보고서가 F001로 단독 검색되는 회사는 반대로 사업보고서 의무가 없는 경우뿐이라 — 두 경로가
    겹치는 회사가 사실상 없어 감사보고서 확인 하나로 먼저 분기하면 충분하다.
    crtfc_key가 설정되어 있다는 전제로 호출된다(호출자가 조건문으로 이미 걸러냄).
    corp_code를 못 찾으면 None. alt_names/known_biz_no/known_ceo_name: get_corp_code 참고.
    """
    async with httpx.AsyncClient() as client:
        corp_code = await get_corp_code(
            client, crtfc_key, corp_name, cache_dir / "dart_corp_codes.csv", alt_names, known_biz_no, known_ceo_name
        )
        if not corp_code:
            return None

        bgn_de = date.today().replace(year=date.today().year - 2).strftime("%Y%m%d")
        audit, consolidated_audit = await asyncio.gather(
            _latest_disclosure(client, crtfc_key, corp_code, "F", "F001", bgn_de),
            _latest_disclosure(client, crtfc_key, corp_code, "F", "F002", bgn_de),
        )

        if audit or consolidated_audit:
            financial_statements: dict[str, dict] = {}
            for disclosure, label in ((audit, "감사보고서"), (consolidated_audit, "연결감사보고서")):
                if not disclosure:
                    continue
                parsed = await _fetch_audit_report_financials(client, crtfc_key, disclosure["rcept_no"])
                if parsed:
                    financial_statements[label] = {"report_nm": disclosure["report_nm"], **parsed}
            return {
                "corp_code": corp_code,
                "audit_report": _disclosure_meta(audit),
                "consolidated_audit_report": _disclosure_meta(consolidated_audit),
                "financial_statements": financial_statements or None,
                "employees": None,
                "indicators": None,
            }

        periodic = await _find_latest_periodic_reports(client, crtfc_key, corp_code, bgn_de)
        # 사업보고서/반기보고서는 내용 구성이 동일해 둘 중 더 최신인 쪽 1건만, 분기보고서는
        # 내용이 상대적으로 적지만 3개월 더 최신이라 항상 별도로 추가(사용자 지시, 2026-07-22).
        annual_or_half_year = max(
            ((periodic[k], k) for k in ("사업보고서", "반기보고서") if k in periodic),
            key=lambda pair: pair[0]["rcept_dt"], default=None,
        )
        latest_quarter = (periodic["분기보고서"], "분기보고서") if "분기보고서" in periodic else None
        periods = [p for p in (annual_or_half_year, latest_quarter) if p]

        financial_statements = {}
        for disclosure, label in periods:
            bsns_year = _extract_bsns_year(disclosure["report_nm"])
            reprt_code = _infer_reprt_code(disclosure["report_nm"])
            if not bsns_year or not reprt_code:
                continue
            statements = await _fetch_full_financials(client, crtfc_key, corp_code, bsns_year, reprt_code)
            if statements:
                financial_statements[label] = {"report_nm": disclosure["report_nm"], "bsns_year": bsns_year, "statements": statements}

        employees = indicators = None
        if annual_or_half_year:
            # 직원현황/재무지표는 분기보고서보다 사업/반기보고서 쪽이 더 완전해서(실측 확인:
            # 분기보고서 기준으로 조회하면 직원현황이 빈 값으로 나옴) 있으면 이쪽을 우선한다.
            disclosure, _label = annual_or_half_year
            bsns_year = _extract_bsns_year(disclosure["report_nm"])
            reprt_code = _infer_reprt_code(disclosure["report_nm"])
            if bsns_year and reprt_code:
                employees_raw = await _get(
                    client, crtfc_key, "empSttus.json",
                    {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code},
                )
                indicator_rows: list[dict] = []
                for idx_cl_code in _IDX_CL_CODES:
                    resp = await _get(
                        client, crtfc_key, "fnlttSinglIndx.json",
                        {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code, "idx_cl_code": idx_cl_code},
                    )
                    if resp and resp.get("list"):
                        indicator_rows.extend(resp["list"])
                employees = dart_normalize.normalize_employees(employees_raw["list"])[0] if employees_raw and employees_raw.get("list") else None
                indicators = dart_normalize.normalize_indicators(indicator_rows)[0]["indicators"] if indicator_rows else None

    return {
        "corp_code": corp_code,
        "audit_report": None,
        "consolidated_audit_report": None,
        "financial_statements": financial_statements or None,
        "employees": employees,
        "indicators": indicators,
    }
