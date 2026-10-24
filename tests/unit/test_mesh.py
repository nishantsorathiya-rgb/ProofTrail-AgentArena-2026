import json
from pathlib import Path

import httpx
import pytest

from app.application.config import Settings
from app.domain.llm import LLMMessage, ModelUnavailable
from app.infrastructure.mesh import MeshLLMAdapter, _MeshOnlyTransport

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "mesh_completion.json").read_text(encoding="utf-8")
)
MESSAGES = [LLMMessage(role="user", content="PRIVATE offline test question")]


def configured_settings(**overrides):
    return Settings(
        _env_file=None, mesh_api_key="offline-test-key", mesh_model_id="fixture-model", **overrides
    )


async def test_sdk_uses_only_mesh_and_configured_model_reuses_client():
    calls = []

    def respond(request):
        calls.append(request)
        assert str(request.url) == "https://api.meshapi.ai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer offline-test-key"
        body = json.loads(request.content)
        assert body["model"] == "fixture-model"
        assert body["messages"][0]["content"] == MESSAGES[0].content
        assert body["max_tokens"] == 1024
        assert all(value == 12 for value in request.extensions["timeout"].values())
        return httpx.Response(200, json=FIXTURE)

    adapter = MeshLLMAdapter(configured_settings(), mock_transport=httpx.MockTransport(respond))
    try:
        result = await adapter.complete(MESSAGES)
        client = adapter._client
        await adapter.complete(MESSAGES)
        assert adapter._client is client
        assert result.text == "Offline fixture response."
        assert result.prompt_tokens == 7
        assert result.completion_tokens == 4
        assert len(calls) == 2
        assert client.timeout == 12
        assert client.max_retries == 0
    finally:
        await adapter.aclose()
    assert client.is_closed()
    with pytest.raises(ModelUnavailable, match="adapter_closed"):
        await adapter.complete(MESSAGES)


async def test_offline_default_blocks_before_client_or_transport_creation():
    adapter = MeshLLMAdapter(configured_settings())
    with pytest.raises(ModelUnavailable, match="live_requests_disabled"):
        await adapter.complete(MESSAGES)
    assert adapter._client is None
    await adapter.aclose()


async def test_mock_still_requires_real_configuration_fields():
    adapter = MeshLLMAdapter(
        Settings(_env_file=None, mesh_api_key="YOUR_MESH_KEY_HERE"),
        mock_transport=httpx.MockTransport(lambda request: pytest.fail("must not be called")),
    )
    with pytest.raises(ModelUnavailable, match="mesh_configuration_missing"):
        await adapter.complete(MESSAGES)
    await adapter.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1/chat/completions",
        "https://api.meshapi.ai.evil.example/v1/chat/completions",
        "https://api.meshapi.ai/v1/responses",
        "https://user:password@api.meshapi.ai/v1/chat/completions",
    ],
)
async def test_transport_rejects_unsafe_routes_before_forwarding(url):
    guard = _MeshOnlyTransport(httpx.MockTransport(lambda request: pytest.fail("route escaped")))
    with pytest.raises(ModelUnavailable, match="mesh_route_rejected"):
        await guard.handle_async_request(httpx.Request("POST", url))
    await guard.aclose()


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "mesh_request_failed"),
        (402, "mesh_request_failed"),
        (429, "mesh_rate_limited"),
        (500, "mesh_request_failed"),
        (302, "mesh_request_failed"),
    ],
)
async def test_provider_errors_are_safe_and_do_not_retry_or_follow_redirects(status, expected):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={
                "Retry-After": "2",
                "Location": "https://api.openai.com/v1/chat/completions",
            },
            json={"error": {"message": "offline-test-key PRIVATE provider payload"}},
        )

    adapter = MeshLLMAdapter(configured_settings(), mock_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ModelUnavailable) as error:
            await adapter.complete(MESSAGES)
        assert error.value.code == expected
        assert "PRIVATE" not in str(error.value)
        assert "offline-test-key" not in str(error.value)
        assert error.value.retry_after_seconds == (2.0 if status == 429 else None)
        assert len(calls) == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "exception,code",
    [
        (httpx.ReadTimeout("PRIVATE timeout"), "mesh_timeout"),
        (httpx.ConnectError("PRIVATE connection"), "mesh_connection_failed"),
    ],
)
async def test_network_errors_are_sanitized(exception, code):
    def respond(request):
        raise exception

    adapter = MeshLLMAdapter(configured_settings(), mock_transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ModelUnavailable, match=code) as error:
            await adapter.complete(MESSAGES)
        assert "PRIVATE" not in str(error.value)
    finally:
        await adapter.aclose()


async def test_absent_token_usage_is_not_invented():
    payload = {key: value for key, value in FIXTURE.items() if key != "usage"}
    adapter = MeshLLMAdapter(
        configured_settings(),
        mock_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload),
        ),
    )
    try:
        result = await adapter.complete(MESSAGES)
        assert result.prompt_tokens is None
        assert result.completion_tokens is None
    finally:
        await adapter.aclose()


async def test_empty_completion_is_a_failure():
    adapter = MeshLLMAdapter(
        configured_settings(),
        mock_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={**FIXTURE, "choices": []}),
        ),
    )
    try:
        with pytest.raises(ModelUnavailable, match="mesh_invalid_response"):
            await adapter.complete(MESSAGES)
    finally:
        await adapter.aclose()
