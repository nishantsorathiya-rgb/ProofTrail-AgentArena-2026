"""Offline retest-launcher guards; fixtures never authorize or measure live providers."""

import copy
import json

import httpx
import pytest

from scripts import session_b_002 as driver
from scripts.offline_quote_locator import HistoricalProviders, fixture_resolver, run_case
from tests.unit.test_session_b_driver import request as old_request


def request(role="planner"):
    prompt = next(prompt for prompt, name in driver.ROLES.items() if name == role)
    data = {"output_schema": {"properties": {"contract_version": {"const": "exact_quotes_v2"}}}}
    return httpx.Request("POST", "https://api.meshapi.ai/v1/chat/completions", json={
        "model": "openai/gpt-4.1-mini", "max_tokens": 4096 if role == "drafter" else 2048,
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": json.dumps(data)}],
    })


@pytest.fixture
def observer(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "JOURNAL", tmp_path / "retest.json")
    return driver.RetestObserver(["offline-private-sentinel"])


@pytest.mark.parametrize("operation,limit", [("mesh", 8), ("search", 2), ("extract", 10)])
async def test_retest_uses_its_own_journal_and_enforces_counts(observer, operation, limit):
    for _ in range(limit):
        await observer.before(request() if operation == "mesh" else old_request(operation))
    with pytest.raises(RuntimeError, match="request_envelope_exhausted"):
        await observer.before(request() if operation == "mesh" else old_request(operation))
    assert observer.record["session"] == "P8 Session B 002"
    assert observer.record["counts"][operation] == limit
    assert json.loads(driver.JOURNAL.read_text())["session"] == "P8 Session B 002"
    assert observer.fatal.is_set()


async def test_retest_cumulative_output_budget_counts_only_permitted_attempts(observer):
    for _ in range(4):
        await observer.before(request("drafter"))
    await observer.before(request())  # 18,432 requested tokens, five permitted attempts.
    with pytest.raises(RuntimeError, match="output_envelope_exhausted"):
        await observer.before(request())
    assert observer.record["requested_output_tokens"] == 18432
    assert observer.record["counts"]["mesh"] == 5


@pytest.mark.parametrize("role", ["primary", "blind_review"])
async def test_retest_requires_v2_schema_for_each_semantic_role(observer, role):
    original = request(role)
    data = json.loads(original.content)
    data["messages"][1]["content"] = json.dumps({
        "output_schema": {"properties": {"contract_version": {"const": "strict_offsets_v1"}}}})
    with pytest.raises(RuntimeError, match="unapproved_verification_contract"):
        await observer.before(httpx.Request("POST", original.url, json=data))
    assert observer.record["counts"]["mesh"] == 0
    assert observer.fatal.is_set()


async def test_retest_rejects_metadata_and_stops_after_refusal(observer):
    with pytest.raises(RuntimeError):
        await observer.before(httpx.Request("GET", "https://api.meshapi.ai/v1/models"))
    assert not observer.record["provider_requests"]
    fresh = driver.RetestObserver([])
    original = request()
    await fresh.before(original)
    await fresh.after(httpx.Response(402, request=original, json={"error": "offline refusal"}))
    with pytest.raises(RuntimeError, match="session_stopped_no_more"):
        await fresh.before(old_request("search"))
    assert fresh.record["counts"] == {"mesh": 1, "search": 0, "extract": 0}


async def test_transport_failure_stops_session_without_logging_message_or_retry(observer):
    calls = 0

    async def failed(req):
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("offline-private-sentinel", request=req)

    transport = driver.StopOnTransportError(httpx.MockTransport(failed), observer)
    with pytest.raises(httpx.ConnectTimeout):
        await transport.handle_async_request(request())
    assert calls == 1 and observer.fatal.is_set()
    assert "offline-private-sentinel" not in driver.JOURNAL.read_text()
    with pytest.raises(RuntimeError, match="session_stopped_no_more"):
        await observer.before(old_request("search"))
    await transport.aclose()


async def test_body_timeout_after_200_still_prevents_another_request(observer):
    class FailedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            raise httpx.ReadTimeout("offline-private-sentinel")

    transport = driver.StopOnTransportError(httpx.MockTransport(
        lambda req: httpx.Response(200, stream=FailedBody(), request=req)), observer)
    response = await transport.handle_async_request(request())
    with pytest.raises(httpx.ReadTimeout):
        await response.aread()
    assert observer.fatal.is_set()
    assert observer.record["stop_reason"] == "provider_body_error_no_further_attempt"
    assert "offline-private-sentinel" not in driver.JOURNAL.read_text()
    await response.aclose()
    await transport.aclose()


@pytest.mark.parametrize("artifact", ["results/p8_session_b_002_execution.json",
    "results/p8_session_b_002_trace.json", "data/p8_session_b_002.db-wal"])
def test_any_old_session_artifact_prevents_replay(tmp_path, monkeypatch, artifact):
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    path = tmp_path / artifact
    path.parent.mkdir(parents=True)
    path.write_text("offline reserved")
    with pytest.raises(ValueError, match="do_not_replay"):
        driver.require_fresh()
    assert path.read_text() == "offline reserved"


def test_offline_check_does_not_construct_app_or_reserve_session(monkeypatch, capsys):
    monkeypatch.setattr(driver, "preflight", lambda: (None, "offline-manifest-digest"))
    monkeypatch.setattr(driver, "create_app", lambda *_: pytest.fail("No app during check"))
    monkeypatch.setattr("sys.argv", ["session_b_002", "--check"])
    assert driver.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["api_requests"] == 0 and output["execution_authorized"] is False
    assert output["dedicated_key_identity_and_quota"] == "OWNER_CONFIRMATION_REQUIRED"


def test_no_mode_or_approval_does_not_call_preflight(monkeypatch):
    monkeypatch.setattr(driver, "preflight", lambda: pytest.fail("No mode, no preparation"))
    monkeypatch.setattr("sys.argv", ["session_b_002"])
    with pytest.raises(SystemExit) as error:
        driver.main()
    assert error.value.code == 2


def test_source_change_and_extra_python_file_are_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(driver, "MANIFEST", manifest)
    source = tmp_path / "app/example.py"
    source.parent.mkdir()
    source.write_text("# offline source\n")
    manifest.write_text(json.dumps({"sha256": {"app/example.py": driver.digest(source)}}))
    assert driver.verify_manifest() == driver.digest(manifest)
    added = tmp_path / "app/added.py"
    added.write_text("# new file\n")
    with pytest.raises(ValueError, match="file_set_changed"):
        driver.verify_manifest()
    added.unlink()
    source.write_text("# changed\n")
    with pytest.raises(ValueError, match="reviewed_source_changed"):
        driver.verify_manifest()


def fixture_trace():
    # Saved offline synthetic positive path, not invented live observations.
    history = json.loads((driver.ROOT / "results/p8_quote_locator_offline_trace.json").read_text())
    case = history["positive"]
    return {"state": case["run"], "claims": case["claims"], "evidence": case["evidence"],
            "report": case["report"], "final_report_repository_validation": "PASSED"}


@pytest.mark.parametrize("missing", [None, "blind_review", "receipt_binding", "complete_report"])
def test_acceptance_demands_resolved_blind_review_and_complete_report(observer, missing):
    trace = fixture_trace()
    observer.record["provider_requests"] = [
        {"role": role, "operation": "mesh", "status_code": 200}
        for role in ("planner", "drafter", "primary", "blind_review", "synthesizer")
    ] + [{"operation": op, "status_code": 200} for op in ("search", "extract")]
    if missing == "blind_review":
        observer.record["provider_requests"] = [r for r in observer.record["provider_requests"]
                                               if r.get("role") != "blind_review"]
    if missing == "receipt_binding":
        trace = copy.deepcopy(trace)
        events = trace["state"]["audit_events"]
        event = next(e for e in events if e["event"] == "quotation_resolution_completed")
        event["details"]["evidence_sha256"] = "changed-fixture-fingerprint"
    if missing == "complete_report":
        trace["report"]["completion_status"] = "partial"
    result = driver.acceptance(trace, observer,
        {"integrity_check": "ok", "foreign_key_violations": [], "alembic_head": "0005"}, "PASSED")
    assert result["passed"] is (missing is None)
    assert result["semantic_accuracy"] == "NOT_MEASURED_BY_ONE_RUN"


async def test_reopen_validates_the_actual_final_reports_table(tmp_path):
    path = tmp_path / "offline.db"
    fixture = await run_case(path)
    settings = driver.Settings(_env_file=None, database_url="sqlite:///" + path.as_posix())
    rows = {"final_reports": [{"run_id": fixture["run"]["run_id"]}]}
    assert await driver.reopened_report(settings, fixture["run"]["run_id"], rows) == "PASSED"


def test_missing_usage_is_unknown_not_free(observer):
    observer.record["provider_requests"] = [{"operation": "mesh", "status_code": 200}]
    summary = driver.usage_summary(observer, {"retrieval_usage": [{"reported_credits": None}]})
    assert summary["usage_based_mesh_cost_estimate_usd"] is None
    assert summary["reported_tavily_credits"] is None
    observer.record["provider_requests"][0]["response_body"] = {
        "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
    summary = driver.usage_summary(observer, {"retrieval_usage": [{"reported_credits": 1}]})
    assert summary["usage_based_mesh_cost_estimate_usd"] == pytest.approx(0.000072)
    assert summary["settled_cost_usd"] is None and summary["reported_tavily_credits"] == 1


@pytest.mark.parametrize("negative", [False, True])
async def test_launcher_exports_one_real_app_fixture_run_without_touching_b001(
    tmp_path, monkeypatch, negative
):
    # Only this test replaces provider transports. Production run() accepts no fixture option.
    # The autouse socket/DNS guard additionally forbids external networking.
    original_factory = driver.create_app
    original_history = driver.JOURNAL.parent / "p8_session_b_001_execution.json"
    historical_hash = driver.digest(original_history)
    for directory in ("data", "results"):
        (tmp_path / directory).mkdir()
    (tmp_path / ".env").write_text("ENABLE_LIVE_REQUESTS=false\n")
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    monkeypatch.setattr(driver, "JOURNAL", tmp_path / "results/p8_session_b_002_execution.json")
    monkeypatch.setattr(driver, "DATABASE", tmp_path / "data/p8_session_b_002.db")
    settings = driver.Settings(_env_file=None, mesh_api_key="offline-private-sentinel",
        tavily_api_key="offline-private-sentinel", mesh_model_id="openai/gpt-4.1-mini", **{
            name: value for name, value in driver.EXPECTED.items()
            if name not in {"mesh_api_key", "tavily_api_key", "mesh_model_id"}})
    monkeypatch.setattr(driver, "preflight", lambda: (settings, "offline-manifest"))
    monkeypatch.setattr(driver, "verify_manifest", lambda: "offline-manifest")

    def offline_factory(configuration):
        providers = HistoricalProviders(negative=negative)
        app = original_factory(configuration, mesh_transport=httpx.MockTransport(providers.mesh),
            tavily_transport=httpx.MockTransport(providers.tavily), resolver=fixture_resolver)
        workflow = app.state.coordinator._workflow
        mesh = workflow._planner._model._model
        tavily = workflow._researcher._collection._search._client
        mesh._get_client()
        tavily._get_client()
        # Test-only facade exercises installation of observer/transport wrappers on clients
        # already holding explicit mocks; no such option exists in the live CLI.
        mesh._mock_transport = tavily._mock = None
        return app

    monkeypatch.setattr(driver, "create_app", offline_factory)
    assert await driver.run() == (1 if negative else 0)
    journal = json.loads(driver.JOURNAL.read_text())
    assert journal["acceptance"]["passed"] is not negative
    assert journal["report_validation_after_database_reopen"] == "PASSED"
    assert journal["private_env_unchanged"] and journal["reviewed_sources_unchanged"]
    assert journal["counts"] == {"mesh": 4 if negative else 5, "search": 1, "extract": 4}
    assert journal["requested_output_tokens"] == (10240 if negative else 12288)
    trace = json.loads((tmp_path / "results/p8_session_b_002_trace.json").read_text())
    assert bool(trace["report"]["verified_findings"]) is not negative
    assert driver.digest(original_history) == historical_hash
    with pytest.raises(ValueError, match="do_not_replay"):
        driver.require_fresh()
