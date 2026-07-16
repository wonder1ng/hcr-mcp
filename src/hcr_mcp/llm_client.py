import base64
import functools
from typing import Any, Callable, Coroutine, TypeVar

import openai
import pydantic
import tiktoken
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

from hcr_mcp.config import Settings
from hcr_mcp.errors import HcrMcpError

_client: AsyncOpenAI | None = None
_embedding_client: AsyncOpenAI | None = None
_chat_model: ChatOpenAI | None = None
_settings: Settings | None = None

T = TypeVar("T")

# OpenAI-Project 헤더가 다른 프로세스/쉘 설정에서 남아있으면 사용자가 설정한 키가 엉뚱한
# 프로젝트로 스코프돼 "키는 맞는데 왜 모델이 안 보이지" 같은 혼란스러운 실패가 난다(사용자
# 실측). 평상시 호출은 정상적인 프로젝트 스코프를 그대로 존중해야 하므로, 이 초기화는
# validate_models()의 최초 검증 호출에만 적용하고 평상시 재사용되는 싱글턴 클라이언트에는
# 적용하지 않는다.
_RESET_PROJECT_HEADER = {"OpenAI-Project": ""}


def init_llm_client(settings: Settings) -> None:
    """서버 시작 시 1회 호출. 이후 지연 초기화(lazy singleton)로 재사용."""
    global _settings
    _settings = settings


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if _settings is None:
            raise HcrMcpError("내부 오류: init_llm_client()가 서버 시작 시 호출되지 않았습니다.")
        _client = AsyncOpenAI(api_key=_settings.llm_api_key)
    return _client


def _get_embedding_client() -> AsyncOpenAI:
    """llm_embedding_api_key가 따로 설정돼 있으면 그 키로, 아니면 llm_api_key를 재사용."""
    global _embedding_client
    if _embedding_client is None:
        if _settings is None:
            raise HcrMcpError("내부 오류: init_llm_client()가 서버 시작 시 호출되지 않았습니다.")
        _embedding_client = AsyncOpenAI(api_key=_settings.llm_embedding_api_key or _settings.llm_api_key)
    return _embedding_client


def get_chat_model() -> ChatOpenAI:
    """LangChain 구조화 출력 체인(fit/service.py 등)에서 쓰는 팩토리."""
    global _chat_model
    if _chat_model is None:
        if _settings is None:
            raise HcrMcpError("내부 오류: init_llm_client()가 서버 시작 시 호출되지 않았습니다.")
        _chat_model = ChatOpenAI(
            model=_settings.llm_chat_model,
            temperature=0,
            api_key=_settings.llm_api_key,
        )
    return _chat_model


def structured_chain(system: str, human: str, schema: type) -> Runnable:
    """system/human 프롬프트 + Pydantic 스키마로 구조화 출력 체인을 만든다.
    fit/service.py, company_report/competitor_finder.py, job_posting/collector.py가 공통으로 쓰는 패턴."""
    prompt = ChatPromptTemplate.from_messages([("system", system), ("human", human)])
    return prompt | get_chat_model().with_structured_output(schema)


def _translate_openai_errors(fn: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
    """openai SDK 예외 + 구조화 출력 검증 실패(pydantic.ValidationError)를 사용자가 바로
    이해할 수 있는 한글 메시지로 변환. safe_ainvoke가 감싸는 structured_chain 호출에서, LLM
    응답이 스키마 제약(예: min_length)을 못 채우면 openai 예외가 아니라 pydantic.ValidationError가
    나서 별도로 잡아야 한다(실측: JobPosting.jobs에 min_length=1을 걸었을 때 이 경로로 확인)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except pydantic.ValidationError as e:
            raise HcrMcpError(f"LLM 응답이 예상한 형식과 맞지 않습니다: {e}") from e
        except openai.AuthenticationError as e:
            raise HcrMcpError(
                "LLM API 키가 유효하지 않습니다. HCR_MCP_LLM_API_KEY 값을 확인하세요."
            ) from e
        except openai.RateLimitError as e:
            raise HcrMcpError(
                "LLM API 요청 한도를 초과했습니다(rate limit 또는 잔액 부족). 잠시 후 다시 시도하거나 사용량/한도를 확인하세요."
            ) from e
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise HcrMcpError(
                f"LLM API 서버에 연결할 수 없습니다: {e}. 네트워크 상태를 확인하세요."
            ) from e
        except openai.BadRequestError as e:
            raise HcrMcpError(f"LLM 요청이 잘못되었습니다: {e}") from e
        except openai.APIError as e:
            raise HcrMcpError(f"LLM API 호출 중 오류가 발생했습니다: {e}") from e

    return wrapper


@_translate_openai_errors
async def chat(messages: list[dict[str, str]], **kwargs: Any) -> str:
    client = _get_client()
    resp = await client.chat.completions.create(
        model=_settings.llm_chat_model,  # type: ignore[union-attr]
        messages=messages,  # type: ignore[arg-type]
        **kwargs,
    )
    return resp.choices[0].message.content or ""


@_translate_openai_errors
async def safe_ainvoke(runnable: Runnable, inputs: dict[str, Any]) -> Any:
    """LangChain 체인(structured_chain 등) 호출의 openai 예외를 명확한 한글 메시지로 변환."""
    return await runnable.ainvoke(inputs)


_EMBED_MAX_TOKENS_PER_REQUEST = 250_000  # OpenAI 실제 요청 한도(300k 토큰)보다 여유를 둔 안전선
_EMBED_ENCODING = "cl100k_base"  # text-embedding-3-* 계열이 쓰는 인코딩(tiktoken 공식 매핑 기준)


def _batch_by_token_limit(texts: list[str], max_tokens: int) -> list[list[str]]:
    """기사(텍스트) 단위로 배치를 나누되(하나의 텍스트를 쪼개지 않음), 각 배치의 누적 토큰
    수가 max_tokens를 넘지 않도록 실제 토큰 수를 세어가며 나눈다. 텍스트를 하나씩 훑으면서
    현재 배치에 더했을 때 한도를 넘으면 그 지점에서 새 배치를 시작한다 — 그래서 유난히 긴
    기사가 섞여 있어도(짧은 기사 위주 배치는 더 많이, 긴 기사가 섞인 배치는 더 적게 묶여)
    그 배치는 자동으로 작아진다. 글자 수 어림(4자≈1토큰 같은 영어권 규칙)은 쓰지 않는다 —
    실측: 이 프로젝트의 기사 본문(한글 위주)은 글자 수와 토큰 수가 거의 1:1이라 어림으로는
    여전히 한도를 넘길 수 있다. tiktoken 인코딩 비용(텍스트당 수백 마이크로초)은 API 왕복
    시간(수백 ms~수 초)에 비해 무시할 수준이라 정확히 세는 쪽을 택했다.

    ponytail: 텍스트 하나가 그 자체로 max_tokens를 넘으면 그 하나만 담은 배치로 분리되지만
    여전히 요청은 실패한다 — 실측 기사 본문 최대치(8,143 토큰)가 이 상한의 3% 수준이라 현재는
    발생하지 않지만, 발생하면 해당 텍스트를 잘라 보내는 로직이 필요하다."""
    enc = tiktoken.get_encoding(_EMBED_ENCODING)
    batches: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for text in texts:
        n = len(enc.encode(text))
        if current and current_tokens + n > max_tokens:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(text)
        current_tokens += n
    if current:
        batches.append(current)
    return batches


@_translate_openai_errors
async def embed_batch(texts: list[str]) -> list[list[float]]:
    """실시간 요청 경로용 배치 임베딩 호출. OpenAI의 요청 크기 한도(300k 토큰/요청)를 넘지
    않도록 _batch_by_token_limit로 미리 쪼개 순차 호출한다 — 텍스트 총량이 많은 호출(예: 뉴스
    기사 다건 dedup용 임베딩)이 한 번에 한도를 넘겨 실패하던 문제(실측)를 여기서 막는다
    (embed_batch를 쓰는 모든 호출부가 자동으로 보호됨)."""
    if not texts:
        return []
    client = _get_embedding_client()
    embeddings: list[list[float]] = []
    for batch in _batch_by_token_limit(texts, _EMBED_MAX_TOKENS_PER_REQUEST):
        resp = await client.embeddings.create(model=_settings.llm_embedding_model, input=batch)  # type: ignore[union-attr]
        embeddings.extend(item.embedding for item in resp.data)
    return embeddings


def _sniff_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    return "image/png"  # 기본값: 컴퓨터 스크린샷 붙여넣기가 가장 흔한 케이스


@_translate_openai_errors
async def vision_extract(images: list[bytes], prompt: str) -> str:
    """스크린샷 여러 장(포맷 무관, 매직바이트로 스니핑)을 한 번의 호출로 함께 넘겨 추출."""
    if not images:
        raise HcrMcpError("vision_extract에 이미지가 하나도 전달되지 않았습니다.")
    client = _get_client()
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_bytes in images:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        mime = _sniff_mime(image_bytes)
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    resp = await client.chat.completions.create(
        model=_settings.llm_chat_model,  # type: ignore[union-attr]
        messages=[{"role": "user", "content": content}],
    )
    return resp.choices[0].message.content or ""


@_translate_openai_errors
async def web_search(query: str) -> str:
    """OpenAI Responses API의 내장 web_search 툴을 사용. 결과 텍스트를 그대로 반환."""
    client = _get_client()
    resp = await client.responses.create(
        model=_settings.llm_chat_model,  # type: ignore[union-attr]
        input=query,
        tools=[{"type": "web_search"}],  # type: ignore[list-item]
    )
    return resp.output_text


_CHECK_LABELS = {
    "chat": "채팅 모델",
    "embedding": "임베딩 모델",
}


async def _check_one(kind: str, model: str, api_key: str) -> str | None:
    """한 항목(채팅 또는 임베딩) 검증. 성공하면 None, 실패하면 사람이 읽을 문제 설명 문자열.
    검증 전용 클라이언트를 매번 새로 만들어 OpenAI-Project 헤더를 초기화한다 — 평상시
    재사용되는 싱글턴 클라이언트(_get_client/_get_embedding_client)는 건드리지 않는다."""
    label = _CHECK_LABELS[kind]
    client = AsyncOpenAI(api_key=api_key, default_headers=_RESET_PROJECT_HEADER)
    try:
        if kind == "chat":
            await client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=1
            )
        else:
            await client.embeddings.create(model=model, input="ping")
    except openai.AuthenticationError:
        return f"{label}({model}): API 키가 유효하지 않습니다."
    except openai.NotFoundError:
        return f"{label}({model}): 이 키로는 해당 모델에 접근할 수 없습니다(모델명 오타이거나, 계정에 이 모델 접근 권한이 없을 수 있습니다)."
    except openai.PermissionDeniedError:
        return f"{label}({model}): 이 키에 접근 권한이 없습니다(조직/프로젝트 설정을 확인하세요)."
    except openai.RateLimitError:
        return f"{label}({model}): 요청 한도 초과(rate limit 또는 잔액 부족)입니다."
    except (openai.APIConnectionError, openai.APITimeoutError) as e:
        return f"{label}({model}): API 서버에 연결할 수 없습니다({e})."
    except openai.APIError as e:
        return f"{label}({model}): 호출 중 오류가 발생했습니다({e})."
    except Exception as e:  # noqa: BLE001 — 예상 못한 예외도 여기서 삼켜야 나머지 검사 항목이 계속 진행된다
        return f"{label}({model}): 예상치 못한 오류로 검증하지 못했습니다\n({e})."
    return None


async def validate_models() -> None:
    """서버 시작 시 1회 — 설정된 채팅/임베딩 모델이 각각의 키로 실제 작동하는지 확인.
    하나가 실패해도 나머지 항목도 계속 검사해서, 실패한 항목을 전부 모아 한 번에 보고한다
    (예: "키는 유효한 OpenAI 키인데 임베딩 모델 접근 권한이 없다"처럼 항목별로 원인이 다를 수 있음)."""
    results = [
        await _check_one("chat", _settings.llm_chat_model, _settings.llm_api_key),  # type: ignore[union-attr]
        await _check_one(
            "embedding",
            _settings.llm_embedding_model,  # type: ignore[union-attr]
            _settings.llm_embedding_api_key or _settings.llm_api_key,  # type: ignore[union-attr]
        ),
    ]
    problems = [r for r in results if r]
    if problems:
        raise HcrMcpError("시작 검증 실패 — 아래 항목을 확인하세요:\n" + "\n".join(f"- {p}" for p in problems))
