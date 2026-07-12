"""회사 뉴스 이슈를 이벤트 유형으로 분류하기 위한 축소 taxonomy.

프로젝트 루트 categoy.json("기업 뉴스 RAG용 이벤트·토픽 분류체계", 취업준비생 관점 중요도
1~10 채점 철학)의 event_taxonomy를 copy-adapt했다. 원본은 11개 대분류 아래 event_type마다
label_en/description/keywords_ko와 별도의 topic_taxonomy·importance_modifiers(사실확정여부·
영향범위·주체여부 배율)까지 포함해 46KB에 달해, 분류 프롬프트에 매번 통째로 넣기엔 너무 크다.
여기서는 분류에 실제로 쓰는 최소 필드(event_id, label_ko, base_importance)만 남겼다.

categoy.json은 hcr-mcp 패키지 밖(워크스페이스 루트)에 있어 설치된 패키지엔 포함되지 않으므로,
이 축소본을 패키지 안에 직접 복사해 둔다 — 다른 콜렉터들이 HcR 원본을 실행 시점에 참조하지
않고 copy-adapt하는 것과 같은 이유.

ponytail: 원본의 factuality_status/scope/company_role 보정 배율(각각 확정·전사적범위·단독주체
여부 등으로 base_importance를 곱해 깎는 장치)은 v1에서 생략했다 — 이슈당 판단 항목이 1개
(event_id)에서 4개로 늘어나는 부담 대비 이득이 적다(우리는 이미 필터링·그룹핑을 거친 소수의
이슈만 다루지, 수천 건을 스코어링하는 대규모 RAG가 아니다). 더 정밀한 랭킹이 필요해지면
categoy.json의 importance_modifiers를 참고해 추가.
"""

EVENT_TAXONOMY: list[dict] = [
    {
        "category_id": "hiring_workforce",
        "label_ko": "채용/인력",
        "event_types": [
            {"event_id": "hiring_workforce.mass_hiring", "label_ko": "대규모 신규채용/공개채용", "base_importance": 9},
            {"event_id": "hiring_workforce.hiring_freeze", "label_ko": "채용 축소/채용 동결", "base_importance": 8},
            {"event_id": "hiring_workforce.layoff_restructuring", "label_ko": "구조조정/정리해고", "base_importance": 10},
            {"event_id": "hiring_workforce.voluntary_retirement", "label_ko": "명예퇴직/희망퇴직", "base_importance": 9},
            {"event_id": "hiring_workforce.special_recruitment", "label_ko": "수시채용/특별채용 확대", "base_importance": 6},
            {"event_id": "hiring_workforce.internship_program", "label_ko": "인턴십/채용연계 프로그램", "base_importance": 7},
        ],
    },
    {
        "category_id": "leadership_governance",
        "label_ko": "경영진/지배구조",
        "event_types": [
            {"event_id": "leadership_governance.ceo_change", "label_ko": "대표이사/CEO 교체", "base_importance": 8},
            {"event_id": "leadership_governance.executive_appointment", "label_ko": "임원/주요보직 인사", "base_importance": 6},
            {"event_id": "leadership_governance.governance_restructuring", "label_ko": "지배구조 개편(지주사 전환 등)", "base_importance": 6},
            {"event_id": "leadership_governance.board_change", "label_ko": "이사회 변화/사외이사 선임", "base_importance": 4},
            {"event_id": "leadership_governance.ownership_change", "label_ko": "최대주주/오너십 변경", "base_importance": 7},
        ],
    },
    {
        "category_id": "business_growth",
        "label_ko": "사업/성장",
        "event_types": [
            {"event_id": "business_growth.new_business_entry", "label_ko": "신사업 진출", "base_importance": 7},
            {"event_id": "business_growth.facility_expansion", "label_ko": "신공장/사업장 설립(국내)", "base_importance": 8},
            {"event_id": "business_growth.overseas_expansion", "label_ko": "해외진출/해외법인 설립", "base_importance": 7},
            {"event_id": "business_growth.business_downsizing", "label_ko": "사업 철수/축소", "base_importance": 8},
            {"event_id": "business_growth.new_product_launch", "label_ko": "신제품/서비스 출시", "base_importance": 5},
            {"event_id": "business_growth.capacity_investment", "label_ko": "설비투자/생산능력 확대", "base_importance": 6},
        ],
    },
    {
        "category_id": "ma_corporate_structure",
        "label_ko": "M&A/기업구조변화",
        "event_types": [
            {"event_id": "ma_corporate_structure.acquisition", "label_ko": "타사 인수", "base_importance": 7},
            {"event_id": "ma_corporate_structure.being_acquired", "label_ko": "피인수/매각 대상", "base_importance": 8},
            {"event_id": "ma_corporate_structure.merger", "label_ko": "합병", "base_importance": 8},
            {"event_id": "ma_corporate_structure.spinoff_split", "label_ko": "분할/스핀오프", "base_importance": 7},
            {"event_id": "ma_corporate_structure.ipo_listing", "label_ko": "상장(IPO)", "base_importance": 7},
            {"event_id": "ma_corporate_structure.delisting", "label_ko": "상장폐지", "base_importance": 8},
            {"event_id": "ma_corporate_structure.bankruptcy_insolvency", "label_ko": "파산/법정관리/회생절차", "base_importance": 10},
            {"event_id": "ma_corporate_structure.subsidiary_change", "label_ko": "자회사 설립/매각/청산", "base_importance": 6},
        ],
    },
    {
        "category_id": "financial_performance",
        "label_ko": "재무/실적",
        "event_types": [
            {"event_id": "financial_performance.earnings_beat", "label_ko": "실적호조/어닝서프라이즈", "base_importance": 6},
            {"event_id": "financial_performance.earnings_miss", "label_ko": "실적부진/적자전환", "base_importance": 8},
            {"event_id": "financial_performance.funding_investment", "label_ko": "투자유치/자금조달", "base_importance": 7},
            {"event_id": "financial_performance.credit_rating_change", "label_ko": "신용등급 변경", "base_importance": 6},
            {"event_id": "financial_performance.liquidity_crisis", "label_ko": "자금난/유동성 위기", "base_importance": 9},
            {"event_id": "financial_performance.dividend_policy", "label_ko": "배당정책 변경", "base_importance": 3},
        ],
    },
    {
        "category_id": "workplace_culture",
        "label_ko": "노동환경/조직문화",
        "event_types": [
            {"event_id": "workplace_culture.labor_strike", "label_ko": "파업/노사분쟁", "base_importance": 8},
            {"event_id": "workplace_culture.industrial_accident", "label_ko": "산업재해/중대재해", "base_importance": 10},
            {"event_id": "workplace_culture.workplace_harassment", "label_ko": "직장 내 괴롭힘/갑질 논란", "base_importance": 8},
            {"event_id": "workplace_culture.welfare_expansion", "label_ko": "복지제도 신설/확대", "base_importance": 6},
            {"event_id": "workplace_culture.work_system_change", "label_ko": "근무제도 변경(주4일제/재택 등)", "base_importance": 7},
            {"event_id": "workplace_culture.labor_agreement", "label_ko": "임금단체협상 타결/결렬", "base_importance": 7},
        ],
    },
    {
        "category_id": "reputation_risk",
        "label_ko": "평판/리스크/준법",
        "event_types": [
            {"event_id": "reputation_risk.embezzlement_scandal", "label_ko": "횡령/배임/오너리스크 형사사건", "base_importance": 9},
            {"event_id": "reputation_risk.regulatory_sanction", "label_ko": "규제위반/과징금/제재", "base_importance": 7},
            {"event_id": "reputation_risk.major_lawsuit", "label_ko": "중대 소송", "base_importance": 6},
            {"event_id": "reputation_risk.product_recall", "label_ko": "제품 리콜/하자", "base_importance": 6},
            {"event_id": "reputation_risk.data_breach", "label_ko": "데이터유출/보안사고", "base_importance": 7},
            {"event_id": "reputation_risk.unfair_practice", "label_ko": "갑질/불공정거래 논란", "base_importance": 7},
            {"event_id": "reputation_risk.environmental_violation", "label_ko": "환경오염/안전기준 위반", "base_importance": 7},
        ],
    },
    {
        "category_id": "industry_market",
        "label_ko": "산업/시장동향",
        "event_types": [
            {"event_id": "industry_market.industry_restructuring", "label_ko": "업계 전반 구조조정", "base_importance": 7},
            {"event_id": "industry_market.policy_regulation_impact", "label_ko": "정책/규제 변화(업계영향)", "base_importance": 6},
            {"event_id": "industry_market.competitor_activity", "label_ko": "경쟁사 동향", "base_importance": 4},
            {"event_id": "industry_market.supply_chain_issue", "label_ko": "공급망 이슈", "base_importance": 5},
        ],
    },
    {
        "category_id": "esg_csr",
        "label_ko": "ESG/사회공헌",
        "event_types": [
            {"event_id": "esg_csr.esg_rating_change", "label_ko": "ESG 평가등급 변화", "base_importance": 4},
            {"event_id": "esg_csr.csr_activity", "label_ko": "사회공헌활동", "base_importance": 3},
            {"event_id": "esg_csr.diversity_inclusion_policy", "label_ko": "다양성/포용 정책", "base_importance": 4},
            {"event_id": "esg_csr.green_management", "label_ko": "친환경경영 선언/이행", "base_importance": 4},
        ],
    },
    {
        "category_id": "tech_rnd",
        "label_ko": "기술/R&D",
        "event_types": [
            {"event_id": "tech_rnd.rnd_investment", "label_ko": "R&D 투자 확대", "base_importance": 6},
            {"event_id": "tech_rnd.patent_filing", "label_ko": "특허 출원/등록", "base_importance": 4},
            {"event_id": "tech_rnd.digital_transformation", "label_ko": "신기술 도입(AI/디지털전환)", "base_importance": 6},
            {"event_id": "tech_rnd.tech_partnership", "label_ko": "기술제휴/라이선싱", "base_importance": 5},
        ],
    },
    {
        "category_id": "partnership_alliance",
        "label_ko": "파트너십/제휴",
        "event_types": [
            {"event_id": "partnership_alliance.strategic_alliance", "label_ko": "전략적 제휴/업무협약(MOU)", "base_importance": 5},
            {"event_id": "partnership_alliance.joint_venture", "label_ko": "합작법인/공동개발", "base_importance": 6},
        ],
    },
    {
        "category_id": "general_other",
        "label_ko": "기타/일반",
        "event_types": [
            {"event_id": "general_other.award_certification", "label_ko": "수상/인증", "base_importance": 3},
            {"event_id": "general_other.event_conference", "label_ko": "행사/컨퍼런스 참가", "base_importance": 2},
            {"event_id": "general_other.general_pr", "label_ko": "단순 홍보/보도자료", "base_importance": 2},
        ],
    },
]

_FALLBACK_EVENT_ID = "general_other.general_pr"

# event_id -> {category_id, category_label, label_ko, base_importance} — 분류 결과 조회용
EVENT_LOOKUP: dict[str, dict] = {
    et["event_id"]: {
        "category_id": cat["category_id"],
        "category_label": cat["label_ko"],
        "label_ko": et["label_ko"],
        "base_importance": et["base_importance"],
    }
    for cat in EVENT_TAXONOMY
    for et in cat["event_types"]
}


def _build_prompt_text() -> str:
    """JSON이 아니라 압축 평문으로 만든다 — 중괄호/따옴표/들여쓰기 같은 JSON 문법 오버헤드 없이
    분류에 필요한 정보(event_id/label_ko/중요도)만 전달해 토큰을 아낀다."""
    lines: list[str] = []
    for cat in EVENT_TAXONOMY:
        lines.append(f'[{cat["category_id"]}] {cat["label_ko"]}')
        for et in cat["event_types"]:
            lines.append(f'  {et["event_id"]}: {et["label_ko"]} (중요도{et["base_importance"]})')
    return "\n".join(lines)


TAXONOMY_PROMPT_TEXT = _build_prompt_text()  # 모듈 로드 시 1회만 생성 — 호출마다 재구성하지 않음
