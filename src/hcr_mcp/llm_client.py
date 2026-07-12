import base64
import functools
from typing import Any, Callable, Coroutine, TypeVar

import openai
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

from hcr_mcp.config import Settings
from hcr_mcp.errors import HcrMcpError

_client: AsyncOpenAI | None = None
_chat_model: ChatOpenAI | None = None
_settings: Settings | None = None

T = TypeVar("T")


def init_llm_client(settings: Settings) -> None:
    """서버 시작 시 1회 호출. 이후 지연 초기화(lazy singleton)로 재사용."""
    global _settings
    _settings = settings


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if _settings is None:
            raise HcrMcpError("내부 오류: init_llm_client()가 서버 시작 시 호출되지 않았습니다.")
        _client = AsyncOpenAI(api_key=_settings.llm_api_key, base_url=_settings.llm_base_url)
    return _client


def get_chat_model() -> ChatOpenAI:
    """LangChain 구조화 출력 체인(fit/service.py 등)에서 쓰는 BYOK 팩토리."""
    global _chat_model
    if _chat_model is None:
        if _settings is None:
            raise HcrMcpError("내부 오류: init_llm_client()가 서버 시작 시 호출되지 않았습니다.")
        _chat_model = ChatOpenAI(
            model=_settings.llm_chat_model,
            temperature=0,
            api_key=_settings.llm_api_key,
            base_url=_settings.llm_base_url,
        )
    return _chat_model


def _translate_openai_errors(fn: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
    """openai SDK 예외를 사용자가 바로 이해할 수 있는 한글 메시지로 변환."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
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
                f"LLM API 서버에 연결할 수 없습니다: {e}. 네트워크 상태 또는 HCR_MCP_LLM_BASE_URL 설정을 확인하세요."
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
async def embed_batch(texts: list[str]) -> list[list[float]]:
    """실시간 요청 경로용 동기(=await 가능한 단발) 임베딩 호출."""
    if not texts:
        return []
    client = _get_client()
    resp = await client.embeddings.create(model=_settings.llm_embedding_model, input=texts)  # type: ignore[union-attr]
    return [item.embedding for item in resp.data]


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
