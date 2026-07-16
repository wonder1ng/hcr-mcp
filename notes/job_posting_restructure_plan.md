# job_posting 패키지 신설 + 채용공고 파서 재작성

## Context

`notes/phase2_plan.md`(2026-07-16 세션)가 남긴 다음 작업은 "`fit/job_collector.py`가
`HcR/hiring_preprocess/clean_all_jobs.py` 패턴을 안 따르고 자유 텍스트만 반환한다"는 문제였다.
이걸 고치려고 코드를 보던 중 사용자가 더 근본적인 문제를 지적했다: **폴더 구조 자체가
기능(priority) 기준이 아니라 아무렇게나 쌓여 있다.** 사용자가 정리한 전체 기능 우선순위는:

1. 채용 공고 + 채용 사이트의 기업정보 입력받고 파싱해서 저장
2. 대상 기업 뉴스, 산업/사업(경쟁사 포함) 뉴스, 직무 최근 동향 수집·저장
3. DART API로 재무정보 조회·저장
4. 회사 분석 보고서
5. 자소서 등 사용자 정보 입력·저장
6. 사용자 정보 × 채용공고 적합도 분석
7. (미착수) AI 면접 및 피드백

지금 `fit/job_collector.py`(채용공고 수집, 1번)가 `fit/`(6번) 밑에 들어가 있고, 채용사이트
기업정보 수집(`company_report/job_site_profile_collector.py`+`jobsite_parsers.py`, 이것도 1번)은
`company_report/`(4번) 밑에 들어가 있다 — 둘 다 "1번: 채용공고+채용사이트 기업정보 수집" 인데
서로 다른 상위 패키지에 흩어져 있고, 그마저 내부적으로 스크래핑/스키마/LLM 정규화 구분 없이
파일 하나에 뭉쳐 있다(`fit/job_collector.py`).

**이번 작업 범위**: 1번 기능("채용공고 및 채용사이트 기업정보 수집·파싱·저장")을 새 최상위
패키지 `job_posting/`으로 통합하고, 그 안에서 관심사(스키마/스크래핑/LLM 정규화/사이트 파서)를
파일로 분리한다. 그리고 원래 요청대로 `job_collector.py`의 파싱 로직을 `clean_all_jobs.py` +
`job_schema_v2.json` 패턴(구조화 JSON, jobs[] 분리 규칙, headcount/deadline 보존 규칙 등)으로
재작성하고, 사용자 지시대로 **부서(department) 필드를 신규 추가**한다.

**범위 밖으로 명시하는 것** (다음 세션에서 별도로 처리):
- 2/3/4번(뉴스·DART·회사분석보고서)을 `company_report/`에서 별도 최상위 패키지로 쪼개는 것.
  지금 `company_report/`에 남는 `news/`, `dart_collector.py`, `dart_normalize.py`,
  `company_profile_collector.py`, `report_builder.py`는 이번엔 그대로 둔다.
- 5번(자소서 등 사용자 정보)을 `fit/`에서 별도 패키지로 쪼개는 것(`resume_collector.py`,
  `resume_schemas.py` 그대로 둠).
- `industry_keyword.py`(부서→사업분야 우선순위 로직) 자체 구현 — `phase2_plan.md`에 이미
  "부서 정보 구조화가 먼저"라고 보류돼 있음. 이번 작업으로 부서 필드가 생기니 그 다음 세션에서
  이어갈 수 있게 된다.

## 새 패키지 구조

```
hcr_mcp/
  job_posting/                    # 신규 — 기능 1번
    __init__.py
    schemas.py                    # job_schema_v2.json 기반 Pydantic (department 필드 추가, TSV 전용 필드 제거)
    collector.py                  # fit/job_collector.py 재작성 이전(rename+rewrite)
    site_profile_collector.py     # company_report/job_site_profile_collector.py 이동(로직 불변)
    site_parsers.py                # company_report/jobsite_parsers.py 이동 (로직 불변)
  fit/
    tool.py                       # job_posting.collector 사용하도록 수정
    service.py                    # 파라미터명 job_doc_text → job_doc_json (내용은 이미 JSON 기대)
    (job_collector.py 삭제)
  company_report/
    (job_site_profile_collector.py, jobsite_parsers.py 삭제 — job_posting/로 이동)
  llm_client.py                   # structured_chain/safe_ainvoke 공용 헬퍼 추가
```

## 세부 변경

### 1. `job_posting/schemas.py` (신규)
`HcR/hiring_preprocess/job_schema_v2.json`을 Pydantic으로 포팅. 변경점:
- 배치용 추적 필드 제거: `raw_meta.source_file`/`source_row`(TSV 행 추적용, 단발 호출엔 의미 없음),
  `preprocess_log.original_text_snapshot`(LLM 캐시 대조용 — 원문은 아래 4번처럼
  `storage.save_raw`로 이미 통째로 보존되므로 중복).
- `raw_meta.ocr_used` → `vision_used`로 개명(이 프로젝트는 EasyOCR이 아니라
  `llm_client.vision_extract`로 스크린샷을 읽음).
- **신규**: `$defs.job`(직무 단위)에 `department: str | None` 추가 — 공고에 언급된
  팀/본부/사업부명을 원문 그대로 추출(부서→사업분야 매핑은 안 함, 그건 미래의
  `industry_keyword.py` 몫). 없으면 null(필수 아님, 사용자 확인사항 반영).

### 2. `job_posting/collector.py` (`fit/job_collector.py` 재작성)
기존 로직(URL 스크래핑 → 부족하면 스크린샷 비전 추출) 유지하되:
1. 스크래핑/비전 추출 직후, LLM 정규화 **이전에** 원문을 `storage.save_raw("job_posting", key,
   "raw_text.txt", ...)`로 즉시 저장 — `report_builder.collect_and_save_news`가 이미 쓰는
   "선별/정규화 실패해도 원문은 남는다" 패턴 재사용(`phase2_plan.md`에 기록된 버그 교훈 재사용).
2. 새 LLM 구조화 호출 추가: `llm_client.structured_chain(system, human, JobPosting)`(아래 3번)로
   `job_schema_v2` 규칙을 이식한 프롬프트 실행. `clean_all_jobs.py`/`preprocessing_notes_v2.md`의
   핵심 규칙을 한국어 프롬프트로 재사용:
   - 모집분야가 여러 개면 반드시 `jobs[]`를 분리, 서로 다른 모집분야의 근무지·자격요건·업무를
     섞지 않음
   - `headcount`("0명" 포함) 원문 그대로 보존, `deadline`은 `YYYY-MM-DD`
   - `education`/`requirements`/`responsibilities`/`preferred` 분류 규칙(v2.2~v2.3에서 정리된 것)
   - 부서(department): 공고에 팀/본부/사업부명이 명시돼 있으면만 추출, 추측 금지(이 프로젝트의
     "주어지지 않은 사실을 지어내지 않는다" 원칙 — `competitor_finder.py`/`news prompts.py`와 동일)
3. 함수 시그니처: `collect_job_posting(job_title, storage: Storage, url=None, screenshot_paths=None) -> JobPosting`
   — `storage` 파라미터 신규 추가(원문 저장 위해 필요). 구조화 결과는
   `storage.save_report("job_posting", key, result)`로 저장 후 반환.

### 3. `llm_client.py`에 공용 헬퍼 추가
`fit/service.py`의 `_chain`/`_safe_ainvoke`를 `llm_client.structured_chain(system, human, schema)`
/`llm_client.safe_ainvoke(runnable, inputs)`로 옮긴다. 지금 이 두 함수와 거의 같은 코드가
`fit/service.py`(원본), `company_report/competitor_finder.py`(인라인 체인 구성, 69~75행)에
중복돼 있음 — 이번에 `job_posting/collector.py`가 세 번째로 같은 걸 필요로 하니 공용화.
`fit/service.py`/`competitor_finder.py` 호출부는 새 헬퍼를 쓰도록 기계적으로 교체(동작 변화 없음).

### 4. `job_posting/site_profile_collector.py`, `job_posting/site_parsers.py`
`company_report/job_site_profile_collector.py`+`jobsite_parsers.py`를 그대로 이동(로직 변경 없음,
내부 import 경로만 `hcr_mcp.job_posting.site_parsers`로 수정). 현재 이 둘은 어디서도 호출되지
않는 고아 코드(향후 `report_builder.py`의 "base 리포트 생성" 단계에서 연결 예정,
`phase2_plan.md` 1-2 항목)라 이동 외 추가 변경 없음 — storage 연동은 그 호출부가 생길 때 같이.

### 5. `fit/tool.py`
```python
from hcr_mcp.job_posting import collector as job_posting_collector
...
posting = await job_posting_collector.collect_job_posting(
    job_title, get_storage(), job_url, job_screenshot_paths
)
job_doc_json = json.dumps({"target_job_title": job_title, **posting.model_dump()}, ensure_ascii=False)
result = await service.analyze_fit(candidate_doc, job_doc_json, company_report)
```
`target_job_title`을 같이 넣는 이유: 기존 코드는 자유 텍스트 맨 앞에 `[직무명]\n{job_title}`을
붙여 LLM에게 "여러 모집분야 중 이게 지원 대상"이라는 신호를 줬다(`JOB_PROFILE_SYSTEM`은 이미
"공고 어느 위치에 있어도" 식으로 유연하게 처리하도록 설계돼 있음) — 구조화 JSON으로 바뀌어도
이 신호가 없어지면 안 되므로 최상위에 유지.

### 6. `fit/service.py`
`analyze_fit(candidate_doc, job_doc_text, company_report)`의 두 번째 파라미터명을
`job_doc_json`으로 변경(내부적으로 `_gen_job_profile`이 이미 `job_doc_json`이라는 이름으로 JSON을
기대하고 있었음 — 이제 이름과 실제 내용이 일치). `JOB_PROFILE_SYSTEM`/`JOB_PROFILE_HUMAN`
프롬프트 자체는 변경 불필요(이미 구조화 입력을 가정한 문구).

## 검증
1. `python -c "import hcr_mcp.job_posting.collector, hcr_mcp.fit.tool"` — import/순환참조 확인.
2. 부서가 여러 개 언급된 실제 공고 URL(예: 네이버 채용 여러 팀 모집 공고)로
   `collect_job_posting` 단독 호출 — `jobs[]`가 모집분야별로 분리되는지, `department`가 팀명
   기준으로 채워지는지, `data/job_posting/{key}/raw/raw_text.txt`와 구조화 JSON이 둘 다
   저장되는지 확인.
3. `analyze_fit` MCP 툴을 이력서+같은 공고 URL로 end-to-end 실행 — 기존과 동일하게 매칭 결과가
   나오는지(구조화 JSON 입력으로 바뀌어도 회귀 없는지) 확인.
4. `notes/phase2_plan.md`에 이번 재구조화 + 다음 우선순위(2/3/4번 패키지 분리는 별도 세션)를
   갱신.
