# Phase 2 (회사 분석 보고서) 구현 계획 — 2026-07-14 세션 정리

이 파일은 새 세션에서 이어서 작업할 때 여기서부터 확인하면 되도록 남기는 현재 상태 스냅샷.
# 우선 plan 모드로 세운 계획 파일(로컬 Claude Code plans 디렉터리)을 읽어 전체 흐름 이해.

## 현재 코드 상태 (2026-07-14  22:27:01 기준)

- Phase 0(스캐폴드), Phase 1(적합도 분석) 완료, MCP 툴 등록까지 끝남(커밋 #1, #2).
- Phase 2 컴포넌트 대부분 존재(커밋 #3): `company_profile_collector.py`, `dart_collector.py`,
  `dart_normalize.py`, `job_site_profile_collector.py`, `jobsite_parsers.py`,
  `news/collector.py`(3종 검색: `collect_recent_issues`/`collect_industry_trend`/`collect_job_trend`,
  실전 검증+디버깅 완료 — `notes/news_collector_investigation.md` 참고), `news/event_taxonomy.py`,
  `schemas.py`, `prompts.py`.
- **비어있는 부분**: `report_builder.py`(조립 단계) 없음, `company_report`가 MCP 툴로 등록 안 됨,
  뉴스 3종 검색 함수들이 실제로 호출되는 곳이 코드 전체에 없음(정의만 있고 고아 상태),
  `storage.save_raw()`도 정의만 있고 호출 0건 — 즉 기사 본문을 수집은 하지만 로컬에 실제로
  남기는 코드가 없음. `chromadb`는 `pyproject.toml`에 있지만 실사용 코드 없음.
- `existing_report_reader.py`는 계획에 있었으나 사용자 지시로 제거됨 — v1은 `hcr-backend`
  DB에 절대 연결 안 함(메모리 `project_hcr_mcp_no_live_db` 참고). base 리포트는 항상 fresh 생성.

## 이번 세션에서 확정한 설계 결정

1. **임베딩/채팅 키 모두 OpenAI 전용으로 고정** (멀티프로바이더 BYOK 폐기).
   이유: v2 공유 캐시 서버가 "LLM 키 없는 단순 스토어"로 설계돼 있어서(원 계획 문서),
   서버가 재임베딩을 할 수 없음 — v1 사용자들의 임베딩이 서로 호환돼야 캐시가 비용 절감
   효과를 낸다. 임베딩만 고정하면 어차피 키를 2개(채팅 아무 프로바이더 + 임베딩 전용
   OpenAI) 넣어야 해서, 그냥 전체를 OpenAI 하나로 단순화하기로 함.
   → `config.py`의 `llm_base_url` 제거, 단일 `llm_api_key`(OpenAI)로 정리 필요.
2. **로컬 벡터DB(Chroma) 도입 안 함.** 리포트 JSON에 이슈 청크 임베딩을 필드로 같이 저장,
   조회 시 `collector.py`의 `_cosine_similarity` 재사용해서 인메모리 스캔.
   이유: v1 스케일(사용자 1명, 회사 단위 리포트, v2 전엔 대량 누적 안 함)에서 순수 파이썬
   스캔으로 충분(수백~1000개 청크까지 sub-second). 텍스트와 벡터가 한 파일에 있어야
   v2 마이그레이션 시 그대로 업로드 가능(별도 export 코드 불필요).
   상한 도달 시 업그레이드 경로: 순수 파이썬 → numpy 벡터화 → (그래도 부족하면) Chroma.
3. **재사용 가치가 있는 자산은 원문 텍스트(수집+정제 비용)이지 임베딩이 아니다** —
   임베딩은 재계산 비용(현금)이 있지만, 원문 보존이 v1→v2 마이그레이션의 핵심.

## 작업 순서 (우선순위 반영, 2026-07-14 확정)

### 0. 설정 단순화 (선행) — 완료 (2026-07-14 23:10:00)
- [x] `config.py`: `llm_base_url` 제거. `llm_api_key`(필수, OpenAI) + `llm_embedding_api_key`(선택,
      생략 시 `llm_api_key` 재사용 — "두 키 쓰고 싶으면 쓰라"는 사용자 결정 반영)
- [x] `llm_client.py`: 임베딩 전용 클라이언트(`_get_embedding_client`) 분리, `embed_batch`가 사용.
      `validate_models()` 신규 — 채팅/임베딩 각각 실제 호출로 검증하고, 하나가 실패해도 나머지도
      계속 검사해서 실패 항목 전부를 한 번에 보고(키 인증 실패 vs 모델 접근 권한 없음 구분).
      `OpenAI-Project` 헤더가 stale하게 남아있으면 설정 변경이 반영 안 되는 것처럼 보이는 문제가
      있어(사용자 실측), **검증 전용 클라이언트에서만** `default_headers={"OpenAI-Project": ""}`로
      강제 초기화(평상시 재사용되는 싱글턴 클라이언트는 건드리지 않음).
- [x] `server.py`: `main()`이 기동 직후 `asyncio.run(llm_client.validate_models())` 호출, 실패 시
      명확한 에러로 즉시 종료.
- [x] README.md 환경변수 표 갱신 (`HCR_MCP_LLM_BASE_URL` 제거 → `HCR_MCP_LLM_EMBEDDING_API_KEY` 추가),
      "뉴스 임베딩은 로컬 Chroma 사용" 문구 삭제(Chroma 도입 안 하기로 확정했으므로)
- [x] `pyproject.toml`에서 미사용 `chromadb>=0.5` 의존성 제거
- (`.env.example` 파일 자체는 레포에 없었음 — 신규 생성은 범위 밖으로 보고 스킵)
- [x] 실제 `.env`(로컬, gitignored) + `server._init()`+`validate_models()` 실행으로 검증 —
      채팅(`gpt-4o-mini`)·임베딩(`text-embedding-3-small`) 둘 다 실제 API 호출로 `200 OK` 확인
      (2026-07-14 23:10:29 기준)

### 1. `report_builder.py` 신규 작성 — **우선순위 1순위: 뉴스 수집 데이터 저장부터**
- [x] **뉴스 연결 + 원문 저장 — 실제 회사(`네이버`, 최악 케이스)로 end-to-end 검증 완료
      (2026-07-15 22:03:39 기준)**: `report_builder.collect_and_save_news()` —
      `collect_recent_issues(회사)` 호출 후 즉시 `storage.save_raw()`로 원문 저장, 이어서
      (industry_keyword 있으면) `collect_industry_trend(산업)`도 같은 방식으로 개별 저장.
      검증 중 발견해 수정한 버그 3개:
      1. raw 저장이 선별(임베딩·그룹핑) *이후*에만 이뤄져, 선별 단계가 실패하면 이미 스크래핑한
         원문까지 통째로 사라지는 구조였음(원래 우선순위 지시의 취지에 반함) → `on_raw_ready`
         콜백을 신설해 `collector._collect_issues`가 매 검색 라운드 스크래핑 직후(그룹핑/임베딩
         시작 전)에 필수 호출하도록 재구조화. `collect_recent_issues`/`collect_industry_trend`/
         `collect_job_trend` 전부 이 콜백을 필수 인자로 받음(생략 불가 — "의무 저장").
      2. `llm_client.embed_batch`가 OpenAI 요청 크기 한도(300k 토큰)를 넘는 입력을 그대로
         보내 크래시(실측: 네이버 583건 기사 임베딩 시도 시 313k 토큰으로 초과). 수정:
         텍스트(기사) 단위로 배치를 나누되, tiktoken으로 실측 토큰 수를 세어 누적 한도(25만
         토큰) 직전에서 새 배치를 시작 — 글자 수 어림(예: 4자≈1토큰)은 이 프로젝트의 한글
         기사 본문에서 부정확함을 실측 확인(글자 수와 토큰 수가 거의 1:1)해서 채택 안 함.
      3. 무관 기사(노이즈) 판정이 임베딩 *이후*(그룹핑 단계)에만 이뤄져, 무관 기사도 본문
         수집+임베딩 대상이 되던 문제 → `_filter_relevant`(제목+스니펫만 사용, structured
         output) 신설해 본문 수집·임베딩보다 먼저 실행. 키워드가 기업/산업/직무 중 무엇인지
         프롬프트에 명시(`_SUBJECT_LABEL`)해 "구름"(기업 Goorm)처럼 일반명사와 표기가 겹치는
         이름 오판을 방지.
      최종 검증 결과: `company_topics: 11`, `industry_topics: 9`, raw json 2개(597KB/533KB)
      로컬 저장 확인.
### 1-1 (신규) 경쟁사 검색 + 산업 키워드 자동 도출 — 2026-07-16 세션에서 착수, 부분 완료
- [x] `competitor_finder.py` 신규 — 경쟁사 후보를 **실제 검색 결과에 근거해서만** 추출(LLM
      자체 지식으로 나열 금지 — `prompts.py`의 "주어지지 않은 사실을 지어내지 않는다" 원칙과
      동일 이유). 소스: 네이버 뉴스검색 1페이지(`news/collector.py`의 검색+파싱 로직 재사용,
      페이지네이션 없음) + `llm_client.web_search`(OpenAI 호스팅 웹검색, 서버사이드라 봇
      차단·JS 렌더링 문제 없음). **구글 검색 HTML 직접 스크래핑은 실측으로 배제** — httpx로는
      실제 결과 없이 JS 챌린지 셸만 반환됨(요청 1건만으로도 재현, 페이지 수·빈도와 무관 —
      "가볍게 1페이지만"으로는 회피 안 됨). 검증: `find_competitors("네이버","포털")` →
      `['카카오','구글','다음','네이트','줌','쿠팡','SSG닷컴','11번가']` (2026-07-15 23:5x 기준).
      아직 `report_builder`에 연결 안 됨.
- [ ] `industry_keyword.py`(산업/사업분야 검색 키워드 자동 도출) — **설계 중 중단, 아래 선행
      작업 필요해서 보류**. 사용자 지적: 네이버처럼 여러 이질적 사업(포털/쇼핑/크림/웹툰/
      브라우저 등)을 겸하는 회사는, 지원자가 지원한 **부서**의 사업 분야를 우선 키워드로 써야
      한다(예: 쇼핑 부서 지원자 → "이커머스", 크림 → "중고거래", 웹툰 → "웹툰", 브라우저 →
      "브라우저"). 부서 정보가 없으면 회사 전체 프로필로 폴백(if 로직이지만, "부서 언급
      여부·회사 전체 사업과 같은 영역인지" 판단 자체가 자유 텍스트 해석이 필요해 파이썬
      분기가 아니라 LLM 프롬프트 우선순위로 처리하기로 함).
      **막힌 지점**: 이 우선순위 로직이 동작하려면 부서 정보가 먼저 파싱돼 있어야 하는데,
      현재 그 어떤 코드도 부서를 구조화해서 뽑아내지 않음 — 아래 신규 발견 참고.

### 신규 발견 (2026-07-16): `fit/job_collector.py`가 프로젝트 관례를 안 따름 — 재작성 필요
`fit/job_collector.py`(Phase 1, 이미 "완료"로 커밋됨)가 채용공고를 구조화하지 않고 그냥
자유 텍스트로만 수집(URL 스크래핑 원문 + 스크린샷 비전 추출 텍스트를 이어붙이기만 함).
이 프로젝트의 다른 콜렉터들(`news/collector.py`는 `HcR/scrapy/news_links_scrapy.py`,
`company_profile_collector.py`는 `HcR/company-crawler/main.py`를 copy-adapt)과 달리
`HcR`의 기존 자산을 참고하지 않고 새로 짠 것으로 보임 — 사용자 지적: "엉망으로 했네."

**참고해야 할 기존 자산** (레포 상위의 `HcR/`, git 추적 대상 아닌 로컬 참고용 레포):
- `jobkorea_posts/*.tsv` — 잡코리아 원본 스크랩(컬럼: company/title/job/detail_text/
  detail_img/detail/etc_info/company_info/url/deadline_date). 파일이 커서(280KB+) 필요한
  부분만 offset/limit으로 읽을 것.
- `hiring_preprocess/clean_all_jobs.py` — OCR+LLM으로 원문을 구조화 스키마로 정제하는
  전처리 스크립트 본체(참고할 실제 로직).
- `hiring_preprocess/job_schema_v2.json` — 목표 스키마(JSON Schema). 공고 레벨(`common`,
  `jobs[]`, `process`, `work_conditions`) + 직무 레벨(`job_name`, `headcount`, `education`,
  `locations`, `responsibilities`, `preferred_common`, `tracks.{newcomer,experienced}`) +
  추적용(`raw_meta`, `preprocess_log`) 구조. **이 스키마에도 부서/division 필드는 없음** —
  사용자 지시: 이 스키마를 기반으로 하되 부서/사업분야 필드는 새로 추가할 것. 해당 값이 부재할 수 있어 필수 값은 아님.
- `hiring_preprocess/preprocessing_notes_v2.md` — v2~v2.4 프롬프트 개선 이력(각 버전에서
  실제로 어떤 파싱 오류가 있었고 어떤 규칙을 추가해 고쳤는지 기록 — 프롬프트 작성 시
  같은 실패를 반복하지 않기 위해 꼭 참고).

**결론/다음 순서**:
1. `fit/job_collector.py`를 `HcR/hiring_preprocess/clean_all_jobs.py` 패턴으로 재작성
   (자유 텍스트 반환 → `job_schema_v2.json` 기반 구조화 JSON 반환) — **부서/사업분야
   필드 신규 추가**. `fit`(Phase 1) 모듈에도 영향 있는 범위가 큰 작업이라 별도로 계획할 것.
2. 구조화된 데이터에서 department를 읽어 `industry_keyword.py`가 우선순위 로직(부서 있으면
   그걸로, 없으면 회사 전체 프로필로 폴백) 완성.
3. `competitor_finder.py`를 `report_builder.collect_and_save_news`에 실제로 연결.
4. `report_builder.py`에 `collect_job_trend`(3번째 뉴스 검색 타입) 연결도 아직 안 됨(기존
   3번 항목과 동일 — job_title이 있을 때 미리 수집해 캐시해두는 용도).

### 1-2. 1.에서 다시 이어서 — **우선순위 1순위: 뉴스 수집 데이터 저장부터**

- [ ] base 리포트 생성: `company_profile_collector` + `dart_collector`/`dart_normalize` → LLM 합성
      → `schemas.py` 형태로
- [ ] 이슈 청크(gist+대표기사 발췌) 임베딩 계산 후 리포트 JSON에 필드로 저장
- [ ] `storage.save_report("company_report", ...)`로 최종 병합 저장


### 2. `company_report`를 MCP 툴로 등록
- [ ] `company_report/tool.py` 신규 (`fit/tool.py` 패턴)
- [ ] `server.py`의 `_init()`에 import 추가

### 3. `fit` 쪽 `collect_job_trend` 연동 확인/연결
- [ ] `fit/service.py`가 `collect_job_trend` 호출하는지 확인(현재 grep 0건 — 미연결로 보임)

### 4. 로컬 RAG 조회 헬퍼
- [ ] `_cosine_similarity`를 공용 위치로 이동
- [ ] 저장된 리포트에서 top-k 조회하는 함수 작성

### 5. 검증
- [ ] 뉴스 3종 검색 실제 호출·저장 확인
- [ ] 기사 본문 로컬 저장 확인 (raw)
- [ ] 3개 연도 버킷 3/31 기준 확인
- [ ] DART 키 있음/없음 섹션 스킵 확인
- [ ] 임베딩 저장 + RAG top-k 조회 확인

## 관련 메모리
프로젝트 메모리 폴더(`C--myfolder-spc-for-cluade-code-hcr-mcp\memory\`)에
`project_hcr_mcp_no_live_db`, `feedback_exception_handling`, `feedback_python_env_conda`,
`feedback_research_caveats` 참고.

## 2026-07-16 세션(2차) — 폴더 구조 재정렬 결정, 구현 전 세션 종료

**중단 사유**: 사용자가 세션을 여기서 끝내야 해서(자야 함), 아래 계획을 실행하지 않고
플랜 파일만 남겨둔 상태. **다음 세션은 여기서부터 시작.**

### 사용자가 확정한 전체 기능 우선순위 (중요 — 앞으로 모든 폴더/모듈 배치의 기준)
지금까지 `fit/`, `company_report/` 두 패키지에 기능이 뒤섞여 있던 것에 대해 사용자가
"기능별로 구분해야지"라며 아래 순번을 명시함. **새 코드를 어디에 둘지 판단할 때 이 번호를
기준으로 삼을 것**:

1. 채용 공고 및 채용 사이트의 기업정보 입력받고 파싱해서 저장하는 기능
2. 대상 기업 뉴스, 산업 및 사업(경쟁사 포함) 뉴스, 해당 직무 최근 동향 정보 수집·저장 기능
3. DART API로 재무 정보 조회·저장하는 기능
4. 회사 분석 보고서 기능
5. 자소서 등 사용자 정보 입력받고 저장하는 기능
6. 사용자 정보와 채용공고 적합도 분석 기능
7. (미착수) 이들을 기반으로 한 AI 면접 및 피드백 기능

원칙: "수집 및 분석하여 생성된 데이터는 전부 저장"(가공 중간 산출물도 포함) — 사용자 명시.
"더 세분화 될 수도 있고 기능 안에서 세분화 될 수도 있다"(번호=상위 패키지 경계, 그 안에서
파일을 더 쪼개는 건 자유) — 사용자 명시.

**현재 구조가 이 번호와 어긋나는 지점**:
- `fit/job_collector.py`(1번 기능)가 `fit/`(6번) 밑에 있음.
- `company_report/job_site_profile_collector.py` + `jobsite_parsers.py`(둘 다 1번 기능,
  채용사이트에서 기업정보 긁어오는 부분)가 `company_report/`(4번) 밑에 있음.
- `company_report/`가 사실 2번(뉴스)+3번(DART)+4번(보고서 조립) 세 기능을 한 패키지에
  뭉쳐 담고 있음.
- `fit/`가 사실 5번(자소서 등 사용자 정보)+6번(적합도 분석) 두 기능을 한 패키지에 뭉쳐 담고 있음.

### 이번 세션에서 세운 계획 (미실행 당시 기록 — 실행 결과는 세션(3차) 참고)
플랜 파일: `notes/job_posting_restructure_plan.md`(레포에 커밋된 원본, 로컬 Claude Code
plans 디렉터리에도 같은 내용 존재).

**이번 계획의 범위**(1번 기능만): 새 최상위 패키지 `hcr_mcp/job_posting/` 신설 —
`schemas.py`(job_schema_v2.json 포팅 + `department` 필드 신규 추가), `collector.py`
(`fit/job_collector.py`를 `HcR/hiring_preprocess/clean_all_jobs.py` 패턴으로 재작성 —
구조화 JSON 반환으로 변경, jobs[] 분리 규칙 등 이식), `site_profile_collector.py` +
`site_parsers.py`(`company_report/job_site_profile_collector.py`+`jobsite_parsers.py` 그대로
이동). 부수 변경: `llm_client.py`에 `structured_chain`/`safe_ainvoke` 공용 헬퍼 추가(현재
`fit/service.py`와 `company_report/competitor_finder.py`에 거의 같은 코드가 중복돼 있어서
공용화), `fit/tool.py`·`fit/service.py`가 새 `job_posting` 패키지를 쓰도록 연결.

**이번 계획에서 명시적으로 범위 밖으로 뺀 것**(다음다음 세션 이후로 미룸):
- 2/3/4번(뉴스/DART/보고서)을 `company_report/`에서 별도 최상위 패키지로 쪼개는 것.
- 5번(자소서 등)을 `fit/`에서 별도 패키지로 쪼개는 것.
- `industry_keyword.py` 자체 구현(부서 필드는 이번에 생기지만 그 다음 로직은 그대로 보류).

### 다음 세션 시작 순서 (당시 기록 — 아래 세션(3차)에서 실행 완료)
1. `notes/job_posting_restructure_plan.md` 플랜 파일 읽기.
2. 사용자에게 그대로 진행할지 확인 후 구현 시작(플랜은 세워졌으나 승인/구현 전에 세션 종료됨).
3. 구현 후 이 섹션과 위쪽 "작업 순서" 체크리스트를 함께 갱신.

## 2026-07-16 세션(3차) — `job_posting/` 패키지 신설 완료

계획대로 구현 전, `HcR/hiring_preprocess/preprocessing_notes_v2.md`(v2.1~v2.4 프롬프트 이력)와
`clean_all_jobs.py`를 직접 대조 검증 — 이력 문서 내용이 최종 프롬프트(v2.7)에 이미 다 반영돼
있어 별도 재해석 없이 최종 프롬프트만 이식하면 됨을 확인. 계획에 없던 추가 발견: LLM이 자주
놓치는 `deadline`/트랙 중복 문제를 잡아주는 `clean_all_jobs.py`의 순수 파이썬 후처리
(`extract_deadline_fallback`, `tracks_are_identical` 경고)는 LLM 호출 비용 없이 재사용 가능해서
계획에 추가해 포팅함(`headcount_value` 숫자 변환은 쓰는 곳이 없어 스킵 — YAGNI).

**완료**:
- `job_posting/schemas.py` — `job_schema_v2.json` → Pydantic 포팅. `department` 필드 신규
  추가(팀/본부/사업부명, 명시된 경우만 추출·추측 금지). 원본의 strict-mode 빈 문자열/배열
  관례 대신 `fit/schemas.py`와 동일하게 진짜 `None` 사용. 배치 전용 필드(`raw_meta.source_file`
  /`source_row`, `preprocess_log.original_text_snapshot`) 제거, `ocr_used`→`vision_used` 개명.
- `job_posting/prompts.py` — `clean_all_jobs.py` 최종 프롬프트(v2.7) 규칙 이식.
- `job_posting/collector.py` — `fit/job_collector.py`(자유 텍스트 반환) 재작성. 원문을
  `storage.save_raw` 즉시 저장 → `llm_client.structured_chain`으로 구조화 → deadline
  정규식 fallback + 트랙 중복 경고 후처리 → `storage.save_report` 저장.
  순수 후처리 로직(`_apply_deadline_fallback`/`_warn_identical_tracks`)은 로컬 assert 기반
  스크립트로 확인(`tests/`는 배포 대상 아니라 gitignore, 레포에는 미포함).
- `job_posting/site_profile_collector.py`, `job_posting/site_parsers.py` — `company_report/`에서
  이동(로직 변경 없음, import만 `hcr_mcp.job_posting.site_parsers`로 수정). 여전히 orphan(호출부
  없음) — base 리포트 생성 단계(아래 1-2 항목)에서 연결 예정.
- `llm_client.py`에 `structured_chain`/`safe_ainvoke` 공용 헬퍼 추가(기존
  `_translate_openai_errors` 데코레이터 재사용, 새 에러 매핑 안 만듦). `fit/service.py`의
  `_chain`/`_safe_ainvoke`, `competitor_finder.py`의 인라인 체인 구성 모두 이걸 쓰도록 교체
  (3중 중복 제거).
- `fit/service.py::analyze_fit` 두 번째 파라미터명 `job_doc_text`→`job_doc_json`.
- `fit/tool.py` — `job_posting.collector.collect_job_posting` 호출, `target_job_title` 포함한
  JSON을 `analyze_fit`에 전달.
- `python -c "import ..."` 임포트 확인 완료(PYTHONPATH=src, study env에 editable install
  안 돼 있어 매번 PYTHONPATH 지정 필요 — 다음 세션 참고).

**미완/미검증(다음 세션)**: 이번 세션 구현은 임포트 확인 + 순수 후처리 로직의 로컬 assert
검증만 마친 상태 — 사용자가 실제 동작을 아직 검증하지 못함. 실제 부서가 여러 개 언급된 공고
URL로 `collect_job_posting` end-to-end 호출 검증(계획 원본 검증 2번), `analyze_fit` MCP 툴
전체 회귀 검증(계획 원본 검증 3번)이 필요 — 둘 다 실제 API 호출 필요해서 이번 세션에서는 안 함.