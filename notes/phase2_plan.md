# Phase 2 (회사 분석 보고서) 구현 계획 — 2026-07-14 세션 정리

이 파일은 새 세션에서 이어서 작업할 때 여기서부터 확인하면 되도록 남기는 현재 상태 스냅샷.

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
- [ ] **(최우선, 완료) 뉴스 연결 + 원문 저장**: `report_builder.collect_and_save_news()` 신규 —
      `collect_recent_issues(회사)` 호출 후 즉시 `storage.save_raw()`로 원문 저장, 이어서
      (industry_keyword 있으면) `collect_industry_trend(산업)`도 같은 방식으로 개별 저장.
      산업 동향 수집이 실패해도 이미 저장된 회사 이슈 원문은 남도록 각 수집 직후 개별 저장.
      아직 실제 회사로 end-to-end 실행 검증은 안 함(다음 세션에서 진행).
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