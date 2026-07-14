# hcr-mcp

취업 분석 MCP 서버 — 회사 분석 보고서 · 적합도 분석 보고서 · AI 면접. 로컬에서 자기 API 키로 실행.

## 설치 및 실행

```
pip install -e .
hcr-mcp
```

MCP 클라이언트(Claude Desktop/Code 등) 설정에 등록할 때 아래 환경변수를 `env`로 전달하세요.

## 환경변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `HCR_MCP_LLM_API_KEY` | 필수 | OpenAI API 키 (채팅+임베딩 공용 기본값) |
| `HCR_MCP_LLM_EMBEDDING_API_KEY` | 선택 | 임베딩 전용 OpenAI 키. 생략 시 `HCR_MCP_LLM_API_KEY` 재사용 |
| `HCR_MCP_LLM_CHAT_MODEL` | 선택 | 기본 `gpt-4o-mini` |
| `HCR_MCP_LLM_EMBEDDING_MODEL` | 선택 | 기본 `text-embedding-3-small` |
| `HCR_MCP_DART_API_KEY` | 선택 | 없으면 회사 분석 보고서의 재무/인력 섹션을 건너뜀 |
| `HCR_MCP_DATA_DIR` | 선택 | 로컬 저장 경로 (기본: `~/.hcr-mcp/data`) |
| `HCR_MCP_DEFAULT_STORAGE_LEVEL` | 선택 | `none` / `results` / `raw` (기본: `results`) |

## 데이터

v1은 LLM/DART API 호출을 제외하면 어떤 서버와도 통신하지 않습니다 — 공유 DB(MariaDB/MongoDB) 없음, 별도 백엔드 서버 없음. 이력서·면접 영상·API 키 등 개인 데이터와 생성된 보고서는 전부 로컬(`HCR_MCP_DATA_DIR`)에 파일로만 저장됩니다(뉴스 이슈 임베딩도 별도 벡터DB 없이 리포트 JSON에 함께 저장, 조회는 인메모리로 계산). 저장 여부와 범위(`none`/`results`/`raw`)는 매 호출 파라미터로 직접 선택합니다.
