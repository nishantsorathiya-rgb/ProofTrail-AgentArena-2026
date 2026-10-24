import json
import subprocess
from decimal import Decimal

import httpx
import pytest
from dotenv import dotenv_values

from app.application.config import Settings
from app.domain.llm import LLMMessage
from app.infrastructure.mesh import MeshLLMAdapter
from scripts import configure_mesh, mesh_smoke

KEY = "rsk_" + "offline_private_fixture_" * 2
METADATA = {
    "id": mesh_smoke.MODEL,
    "supports_structured_output": True,
    "supports_system_prompt": True,
    "supports_completions_api": True,
    "pricing": {
        "pricing_unit": "per_1m_tokens",
        "prompt_usd_per_1m": "0.40",
        "completion_usd_per_1m": "1.60",
    },
}


def settings():
    return Settings(_env_file=None, mesh_api_key=KEY, mesh_model_id=mesh_smoke.MODEL)


def completion(**overrides):
    return {
        "id": "fixture-only",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4.1-mini-2025-04-14",
        "choices": [{
            "index": 0, "message": {"role": "assistant", "content": '{"status":"ok"}'},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45},
        **overrides,
    }


async def run(handler, **overrides):
    return await mesh_smoke.execute(
        settings(), approved_live=True, max_usd=Decimal("0.01"), confirmed_key_limits=True,
        mock_transport=httpx.MockTransport(handler), **overrides,
    )


def test_preflight_never_constructs_network_client(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("preflight network"))
    result = mesh_smoke.preflight(settings())
    assert result["live_requests_executed"] == 0
    assert result["authorized_spending_usd"] == "0"
    assert Decimal(result["maximum_estimated_completion_cost_usd"]) == Decimal("0.0004096")
    assert KEY not in json.dumps(result)


def test_batch_tariffs_are_not_additional_fees_for_synchronous_smoke():
    payload = {**METADATA, "pricing": {
        **METADATA["pricing"], "batch_input_usd_per_1m": "0.20",
        "batch_output_usd_per_1m": "0.80",
    }}
    assert mesh_smoke.validate_prices(payload) == (Decimal("0.40"), Decimal("1.60"))
    with pytest.raises(mesh_smoke.SmokeRejected, match="additional_pricing_dimension"):
        mesh_smoke.validate_prices({**payload, "pricing": {**payload["pricing"], "request_usd": 1}})


def test_invalid_batch_tariff_is_still_rejected():
    payload = {**METADATA, "pricing": {
        **METADATA["pricing"], "batch_input_usd_per_1m": "NaN",
    }}
    with pytest.raises(mesh_smoke.SmokeRejected, match="model_metadata_invalid"):
        mesh_smoke.validate_prices(payload)


@pytest.mark.parametrize("changes", [
    {"approved_live": False}, {"confirmed_key_limits": False},
    {"max_usd": Decimal("0.1")}, {"max_usd": Decimal("0")},
    {"max_usd": Decimal("NaN")}, {"settings": Settings(_env_file=None)},
])
async def test_approval_cap_and_configuration_guards_precede_network(changes, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("premature client"))
    arguments = {
        "settings": settings(), "approved_live": True, "max_usd": Decimal("0.01"),
        "confirmed_key_limits": True, **changes,
    }
    with pytest.raises(mesh_smoke.SmokeRejected):
        await mesh_smoke.execute(**arguments)


async def test_success_uses_existing_adapter_fixed_prompt_and_two_requests():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer " + KEY
        assert all(value == 12 for value in request.extensions["timeout"].values())
        if request.method == "GET":
            assert str(request.url) == mesh_smoke.MODEL_URL
            return httpx.Response(200, json=METADATA)
        assert str(request.url) == "https://api.meshapi.ai/v1/chat/completions"
        assert request.headers["X-Mesh-Cache"] == "no-store"
        body = json.loads(request.content)
        assert body["max_tokens"] == 128
        assert body["model"] == mesh_smoke.MODEL
        assert body["temperature"] == 0
        assert body["messages"] == [
            {"role": message.role, "content": message.content} for message in mesh_smoke.MESSAGES
        ]
        return httpx.Response(200, json=completion())

    result = await run(handler)
    assert len(calls) == 2
    assert result["mode"] == "OFFLINE_MOCK"
    assert result["passed"]
    assert result["prompt_tokens"] == 40
    assert Decimal(result["estimated_usage_cost_usd"]) == Decimal("0.000024")
    assert result["actual_billed_cost_usd"] is None
    assert result["semantic_accuracy_measured"] is False
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("changes", [
    {"id": "another-model"}, {"supports_structured_output": False},
    {"pricing": {}},
    {"pricing": {**METADATA["pricing"], "prompt_usd_per_1m": "0.41"}},
    {"pricing": {**METADATA["pricing"], "completion_usd_per_1m": "NaN"}},
    {"pricing": {**METADATA["pricing"], "request_usd": "0.01"}},
])
async def test_unconfirmed_models_or_prices_stop_before_completion(changes):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={**METADATA, **changes})

    result = await run(handler)
    assert not result["passed"]
    assert result["completion_requests"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("stage,status", [
    ("metadata", 401), ("metadata", 302), ("metadata", 429),
    ("completion", 402), ("completion", 429), ("completion", 500),
])
async def test_provider_errors_have_no_retry_redirect_or_secret_leak(stage, status, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET" and stage == "completion":
            return httpx.Response(200, json=METADATA)
        return httpx.Response(
            status, json={"error": {"message": KEY + " PRIVATE"}},
            headers={"Location": "https://api.openai.com/v1/models", "Retry-After": "1"},
        )

    result = await run(handler)
    assert not result["passed"]
    assert len(calls) == (1 if stage == "metadata" else 2)
    output = json.dumps(result) + str(capsys.readouterr())
    assert KEY not in output and "PRIVATE" not in output


@pytest.mark.parametrize("stage", ["metadata", "completion"])
async def test_timeouts_fail_safely_without_retry(stage):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET" and stage == "completion":
            return httpx.Response(200, json=METADATA)
        raise httpx.ReadTimeout(KEY + " PRIVATE")

    result = await run(handler)
    assert not result["passed"]
    assert result["error_code"] in {"mesh_timeout", "metadata_timeout"}
    assert len(calls) == (1 if stage == "metadata" else 2)


@pytest.mark.parametrize("payload", [
    completion(model="wrong-model PRIVATE"), completion(usage=None),
    completion(usage={"prompt_tokens": 513, "completion_tokens": 5, "total_tokens": 518}),
    completion(choices=[{
        "index": 0, "message": {"role": "assistant", "content": '{"status":"ok","new":1}'},
        "finish_reason": "stop",
    }]),
])
async def test_invalid_response_cannot_be_a_success(payload):
    def handler(request):
        return httpx.Response(200, json=METADATA if request.method == "GET" else payload)

    result = await run(handler)
    assert not result["passed"]
    assert "PRIVATE" not in json.dumps(result)


async def test_cache_bypass_is_opt_in_and_preserves_default_adapter_behavior():
    def handler(request):
        assert "X-Mesh-Cache" not in request.headers
        return httpx.Response(200, json=completion())

    adapter = MeshLLMAdapter(settings(), mock_transport=httpx.MockTransport(handler))
    try:
        await adapter.complete([LLMMessage("user", "fixture")])
    finally:
        await adapter.aclose()


@pytest.fixture
def private_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    return tmp_path


def test_prepare_preserves_existing_keys_and_keeps_application_offline(private_repo):
    target = private_repo / ".env"
    target.write_text(f"MESH_API_KEY='{KEY}'\nTAVILY_API_KEY='private-fixture'\n", encoding="utf-8")
    configure_mesh.prepare(private_repo)
    values = dotenv_values(target)
    assert values["MESH_API_KEY"] == KEY
    assert values["TAVILY_API_KEY"] == "private-fixture"
    assert values["ENABLE_LIVE_REQUESTS"] == "false"
    assert values["MESH_MODEL_ID"] == mesh_smoke.MODEL


def test_key_entry_requires_a_masked_interactive_terminal(private_repo, monkeypatch):
    monkeypatch.setattr(configure_mesh.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(configure_mesh.getpass, "getpass", lambda _: pytest.fail("unmasked entry"))
    with pytest.raises(ValueError, match="interactive"):
        configure_mesh.enter_key(private_repo)
    assert not (private_repo / ".env").exists()


def test_private_key_entry_never_prints_key_and_disables_live(private_repo, monkeypatch, capsys):
    monkeypatch.setattr(configure_mesh.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(configure_mesh.getpass, "getpass", lambda _: KEY)
    configure_mesh.enter_key(private_repo)
    assert dotenv_values(private_repo / ".env")["MESH_API_KEY"] == KEY
    assert dotenv_values(private_repo / ".env")["ENABLE_LIVE_REQUESTS"] == "false"
    assert KEY not in str(capsys.readouterr())


def test_configuration_refuses_a_tracked_env(private_repo):
    target = private_repo / ".env"
    target.write_text("placeholder", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".env"], cwd=private_repo, check=True, capture_output=True)
    with pytest.raises(ValueError, match="untracked"):
        configure_mesh.prepare(private_repo)
    assert target.read_text(encoding="utf-8") == "placeholder"
