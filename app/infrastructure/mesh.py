import logging
from typing import cast

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.application.config import Settings
from app.domain.llm import LLMMessage, LLMResult, ModelUnavailable
from app.infrastructure.observability import configure_logging

logger = logging.getLogger("prooftrail")


class _MeshOnlyTransport(httpx.AsyncBaseTransport):
    """Check every outbound SDK request, including attempts to change its base URL."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (
            url.scheme != "https"
            or url.host != "api.meshapi.ai"
            or url.port not in {None, 443}
            or url.path != "/v1/chat/completions"
            or url.username
            or url.password
            or url.query
        ):
            raise ModelUnavailable("mesh_route_rejected")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class MeshLLMAdapter:
    """Sole runtime model adapter. Injected MockTransport is strictly offline."""

    def __init__(
        self,
        settings: Settings,
        *,
        mock_transport: httpx.MockTransport | None = None,
        disable_response_cache: bool = False,
    ):
        configure_logging()
        if mock_transport is not None and not isinstance(mock_transport, httpx.MockTransport):
            raise TypeError("Only httpx.MockTransport may bypass the offline default.")
        self._settings = settings
        self._mock_transport = mock_transport
        self._disable_response_cache = disable_response_cache
        self._client: AsyncOpenAI | None = None
        self._closed = False

    def _get_client(self) -> AsyncOpenAI:
        if self._closed:
            raise ModelUnavailable("adapter_closed")
        if self._mock_transport is None and not self._settings.enable_live_requests:
            raise ModelUnavailable("live_requests_disabled")
        key = self._settings.mesh_api_key
        model = self._settings.mesh_model_id
        if (
            key is None
            or not key.get_secret_value().strip()
            or key.get_secret_value() == "YOUR_MESH_KEY_HERE"
            or not model
            or not model.strip()
            or model == "MODEL_ID_FROM_YOUR_MESH_DASHBOARD"
        ):
            raise ModelUnavailable("mesh_configuration_missing")
        if self._client is None:
            transport = _MeshOnlyTransport(
                self._mock_transport or httpx.AsyncHTTPTransport(retries=0)
            )
            http_client = httpx.AsyncClient(
                transport=transport,
                timeout=self._settings.http_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            )
            self._client = AsyncOpenAI(
                base_url=self._settings.mesh_base_url,
                api_key=key.get_secret_value(),
                http_client=http_client,
                timeout=self._settings.http_timeout_seconds,
                max_retries=0,
            )
        return self._client

    async def complete(self, messages: list[LLMMessage], *, max_tokens: int = 1024) -> LLMResult:
        if not messages or not 1 <= max_tokens <= 4096:
            raise ValueError("Provide messages and an output limit from 1 to 4096 tokens.")
        client = self._get_client()
        payload = cast(
            list[ChatCompletionMessageParam],
            [{"role": message.role, "content": message.content} for message in messages],
        )
        try:
            response = await client.chat.completions.create(
                model=self._settings.mesh_model_id,
                messages=payload,
                max_tokens=max_tokens,
                temperature=0,
                extra_headers=(
                    {"X-Mesh-Cache": "no-store"} if self._disable_response_cache else None
                ),
            )
            text = response.choices[0].message.content
            if not isinstance(text, str) or not text.strip():
                raise ModelUnavailable("mesh_invalid_response")
            usage = response.usage
            result = LLMResult(
                text=text,
                model=response.model,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
            )
        except APITimeoutError:
            raise ModelUnavailable("mesh_timeout") from None
        except APIConnectionError:
            raise ModelUnavailable("mesh_connection_failed") from None
        except APIStatusError as error:
            retry_after = None
            if error.status_code == 429:
                try:
                    retry_after = min(300.0, max(0.0, float(error.response.headers["retry-after"])))
                except (KeyError, ValueError):
                    pass
            code = "mesh_rate_limited" if error.status_code == 429 else "mesh_request_failed"
            raise ModelUnavailable(code, retry_after) from None
        except (ValueError, TypeError, AttributeError, IndexError):
            raise ModelUnavailable("mesh_invalid_response") from None
        logger.info(
            "mesh_completion",
            extra={
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            },
        )
        return result

    async def aclose(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.close()
