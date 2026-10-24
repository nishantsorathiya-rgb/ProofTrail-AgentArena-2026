"""Single-use driver for the owner's explicitly approved Session B 001 envelope.

No provider request occurs on import. The existing app, graph, adapters and prompts
are used unchanged. HTTPX hooks only observe/enforce the approved outer envelope.
The exclusive journal prevents replay, including after a failed or interrupted run.
"""

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid5

import httpx

from app.application.config import Settings
from app.application.reports import markdown_report
from app.domain.retrieval import passages_for
from app.infrastructure.claim_repository import SQLiteClaimRepository
from app.infrastructure.database import Database
from app.infrastructure.report_repository import SQLiteReportRepository
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "results/p8_session_b_001_execution.json"
DATABASE = ROOT / "data/p8_session_b_001.db"
QUESTION = (
    "According to Python's official documentation, in which Python version was tomllib "
    "added to the standard library?"
)


def now():
    return datetime.now(UTC).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight():
    private = json.loads((ROOT / "data/p8_session_b_preflight_001_private.json").read_text())
    settings = Settings()
    expected = {
        "mesh_base_url": "https://api.meshapi.ai/v1", "mesh_model_id": "openai/gpt-4.1-mini",
        "enable_live_requests": False, "max_llm_calls_per_run": 8,
        "max_searches_per_run": 2, "max_fetches_per_run": 2, "max_claims_per_run": 1,
        "max_research_subquestions": 1, "max_research_tasks_per_run": 2,
        "max_retries_per_subquestion": 1, "max_parallel_fetches": 1,
        "max_parallel_research_tasks": 1, "max_parallel_verifications": 1,
        "max_active_runs": 1, "max_pending_runs": 0, "tavily_max_attempts": 1,
        "http_timeout_seconds": 12, "workflow_timeout_seconds": 120,
    }
    if any(getattr(settings, key) != value for key, value in expected.items()):
        raise ValueError("local_settings_changed")
    if digest(ROOT / ".env") != private["env_sha256"]:
        raise ValueError("private_configuration_changed")
    if any(digest(ROOT / name) != value for name, value in private["source_sha256"].items()):
        raise ValueError("application_source_changed")
    for key, pattern in (
        (settings.mesh_api_key, r"rsk_[A-Za-z0-9_-]{16,512}"),
        (settings.tavily_api_key, r"tvly-[A-Za-z0-9_-]{16,512}"),
    ):
        if key is None or not re.fullmatch(pattern, key.get_secret_value()):
            raise ValueError("private_key_not_configured")
    if subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=ROOT, capture_output=True
    ).returncode != 0 or subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=ROOT, capture_output=True
    ).returncode != 1:
        raise ValueError("private_env_not_excluded")
    if JOURNAL.exists() or DATABASE.exists():
        raise ValueError("session_already_reserved_do_not_replay")
    return settings, private


class SessionObserver:
    def __init__(self, secrets):
        self.secrets = secrets
        self.fatal = asyncio.Event()
        self.responses = []
        self.starts = {}
        self.record = {
            "session": "P8 Session B 001", "mode": "REAL_MESH_AND_TAVILY",
            "started_at_utc": now(), "status": "RESERVED_NOT_SUBMITTED",
            "owner_approval": "Explicit single-run approval in this chat; unknown higher-scope "
            "capacity and soft spending/token limitations accepted. Dedicated key unused "
            "and exclusive use confirmed by owner.",
            "question": QUESTION,
            "limits": {"mesh": 8, "search": 2, "extract": 10,
                       "requested_output_tokens": 18432, "research_retries": 1,
                       "free_tavily_credit_reserve": 12, "transport_retries": 0,
                       "provider_fallback": False, "metadata_and_billing_requests": 0},
            "counts": {"mesh": 0, "search": 0, "extract": 0},
            "requested_output_tokens": 0, "provider_requests": [],
            "higher_scope_capacity_verified": False, "settled_cost_usd": None,
            "private_env_changed": False, "pushed": False, "deployed": False,
            "submitted": False,
        }

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return re.sub(r"\b(?:rsk_|tvly-)[A-Za-z0-9_-]{16,}", "[REDACTED]", value)
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        return value

    def capture_consumed_mesh_responses(self):
        for item, response in self.responses:
            if item["operation"] == "mesh" and response.is_stream_consumed:
                try:
                    item["response_body"] = self.redact(response.json())
                except (ValueError, httpx.ResponseNotRead):
                    item["response_body_unavailable"] = True

    def save(self):
        self.capture_consumed_mesh_responses()
        JOURNAL.write_text(json.dumps(self.redact(self.record), indent=2) + "\n", encoding="utf-8")

    async def before(self, request):
        if self.fatal.is_set():
            raise RuntimeError("session_stopped_no_more_provider_requests")
        payload = json.loads(request.content)
        if request.method != "POST" or request.url.scheme != "https":
            raise RuntimeError("unapproved_provider_request")
        output = 0
        if request.url.host == "api.meshapi.ai" and request.url.path == "/v1/chat/completions":
            operation = "mesh"
            output = payload.get("max_tokens", 0)
            if payload.get("model") != "openai/gpt-4.1-mini" or not 1 <= output <= 4096:
                raise RuntimeError("unapproved_mesh_configuration")
            if self.record["requested_output_tokens"] + output > 18432:
                self.fatal.set()
                raise RuntimeError("session_output_envelope_exhausted")
        elif request.url.host == "api.tavily.com" and request.url.path in {"/search", "/extract"}:
            operation = request.url.path[1:]
            if operation == "search" and (
                payload.get("search_depth") != "basic" or payload.get("include_answer") is not False
                or payload.get("max_results") != 5
            ):
                raise RuntimeError("unapproved_search_configuration")
            if operation == "extract" and (
                payload.get("extract_depth") != "basic" or len(payload.get("urls", [])) != 1
            ):
                raise RuntimeError("unapproved_extract_configuration")
        else:
            raise RuntimeError("unapproved_provider_endpoint")
        if self.record["counts"][operation] >= self.record["limits"][operation]:
            self.fatal.set()
            raise RuntimeError("session_request_envelope_exhausted")
        self.record["counts"][operation] += 1
        self.record["requested_output_tokens"] += output
        item = {"sequence": len(self.record["provider_requests"]) + 1,
                "operation": operation, "started_at_utc": now(),
                "request_body": self.redact(payload), "status_code": None}
        self.record["provider_requests"].append(item)
        self.starts[id(request)] = (item, perf_counter())
        self.save()  # Reserve on disk before the actual HTTP attempt.

    async def after(self, response):
        item, started = self.starts[id(response.request)]
        item.update(status_code=response.status_code,
                    headers_received_at_utc=now(),
                    latency_to_headers_seconds=round(perf_counter() - started, 6),
                    response_headers={name: response.headers[name] for name in (
                        "x-request-id", "x-mesh-request-id", "x-mesh-provider",
                        "x-mesh-attempts", "x-mesh-retries", "x-mesh-fallback",
                        "x-mesh-cost", "retry-after",
                    ) if name in response.headers})
        self.responses.append((item, response))
        if response.status_code >= 400:
            self.record["stop_reason"] = "provider_http_error_no_further_attempt"
            self.fatal.set()
        self.save()
        print(f"Provider response: {item['operation']} HTTP {response.status_code}", flush=True)


def sql_export():
    with sqlite3.connect(DATABASE) as connection:
        connection.row_factory = sqlite3.Row
        tables = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        rows = {table: [dict(r) for r in connection.execute(f'SELECT * FROM "{table}"')]
                for table in tables}
        integrity = {"alembic_head": connection.execute(
            "SELECT version_num FROM alembic_version").fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": [
                tuple(r) for r in connection.execute("PRAGMA foreign_key_check")
            ]}
    return rows, integrity


async def run():
    settings, private = preflight()
    secrets = [settings.mesh_api_key.get_secret_value(), settings.tavily_api_key.get_secret_value()]
    observer = SessionObserver(secrets)
    with JOURNAL.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(observer.record, indent=2) + "\n")
    with DATABASE.open("xb"):
        pass
    started = perf_counter()
    configuration = settings.model_copy(update={
        "enable_live_requests": True, "max_fetches_per_run": 10,
        "workflow_timeout_seconds": 300,
        "database_url": "sqlite:///" + DATABASE.as_posix(),
    })
    observer.record["driver_sha256"] = digest(ROOT / "scripts/session_b_live.py")
    observer.record["process_configuration"] = {
        name: getattr(configuration, name) for name in (
            "mesh_base_url", "mesh_model_id", "enable_live_requests",
            "max_llm_calls_per_run", "max_searches_per_run", "max_fetches_per_run",
            "max_claims_per_run", "max_research_subquestions", "max_research_tasks_per_run",
            "max_retries_per_subquestion", "max_parallel_fetches",
            "max_parallel_research_tasks", "max_parallel_verifications",
            "max_active_runs", "max_pending_runs", "tavily_max_attempts",
            "http_timeout_seconds", "workflow_timeout_seconds",
        )
    }
    observer.save()
    app = create_app(configuration)  # No mock transport, resolver override or altered graph.
    coordinator = app.state.coordinator
    workflow = coordinator._workflow
    mesh = workflow._planner._model._model
    tavily = workflow._researcher._collection._search._client
    sdk = mesh._get_client()  # Client construction only, not authentication or an API call.
    assert sdk.max_retries == 0 and mesh._mock_transport is None and tavily._mock is None
    for client in (sdk._client, tavily._get_client()):
        client.event_hooks["request"].append(observer.before)
        client.event_hooks["response"].append(observer.after)
    run_id = None
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://prooftrail.local"
            ) as local:
                accepted = await local.post("/research", json={"question": QUESTION})
                observer.record["submission"] = {
                    "status_code": accepted.status_code, "body": accepted.json(),
                    "research_posts": 1,
                }
                if accepted.status_code != 202:
                    raise RuntimeError("research_submission_not_accepted")
                run_id = UUID(accepted.json()["run_id"])
                observer.record.update(run_id=str(run_id), status="RUNNING")
                observer.save()
                idle = asyncio.create_task(coordinator.wait_idle())
                halt = asyncio.create_task(observer.fatal.wait())
                done, _pending = await asyncio.wait(
                    {idle, halt}, return_when=asyncio.FIRST_COMPLETED)
                if halt in done and observer.fatal.is_set():
                    await coordinator.aclose()
                await idle
                halt.cancel()
                await asyncio.gather(halt, return_exceptions=True)
                state = await coordinator.get(run_id)
                claims = await coordinator.claims(run_id)
                evidence = await coordinator.evidence(run_id)
                trace = {"graph": workflow.graph.get_graph().to_json(),
                         "state": state.model_dump(mode="json"),
                         "claims": claims.model_dump(mode="json"),
                         "evidence": evidence.model_dump(mode="json"),
                         "report": None}
                try:
                    report = await coordinator.report(run_id)
                    if report is not None:
                        await coordinator._reports.validate(report)
                        trace["report"] = report.model_dump(mode="json")
                        trace["final_report_repository_validation"] = "PASSED"
                        (ROOT / "results/p8_session_b_001_report.md").write_text(
                            observer.redact(markdown_report(report)), encoding="utf-8")
                except Exception as error:
                    trace["report_error_type"] = type(error).__name__
                    trace["report_error_code"] = getattr(error, "code", "unavailable")
                for source in evidence.sources:
                    assert (
                        hashlib.sha256(source.content.encode()).hexdigest() == source.content_sha256
                    )
                    expected_id = uuid5(run_id, f"{source.normalized_url}\n"
                                        f"{source.full_content_sha256}\n{source.content_sha256}\n"
                                        f"{source.retrieved_at.astimezone(UTC).isoformat()}")
                    assert source.source_id == expected_id
                    stored = [p for p in evidence.passages if p.source_id == source.source_id]
                    assert {p.passage_id for p in stored} == {
                        p.passage_id for p in passages_for(source)
                    }
                    assert all(source.content[p.start_char:p.end_char] == p.text for p in stored)
                trace["source_hash_passage_and_stable_id_checks"] = "PASSED"
                observer.record.update(workflow_status=state.status,
                                       execution_status=state.execution_status,
                                       retry_count=len(state.retry_history))
                (ROOT / "results/p8_session_b_001_trace.json").write_text(
                    json.dumps(observer.redact(trace), indent=2) + "\n", encoding="utf-8")
    except Exception as error:
        observer.record["driver_error"] = {"type": type(error).__name__,
                                           "code": getattr(error, "code", "driver_error")}
    finally:
        observer.record.update(finished_at_utc=now(),
                               total_driver_duration_seconds=round(perf_counter() - started, 6))
        observer.record["private_env_unchanged"] = digest(ROOT / ".env") == private["env_sha256"]
        observer.record["original_sources_unchanged"] = all(
            digest(ROOT / name) == value for name, value in private["source_sha256"].items())
        observer.record["status"] = "STOPPED_AFTER_SINGLE_EXECUTION"
        observer.save()
    rows, integrity = sql_export()
    observer.record["database_integrity"] = integrity
    (ROOT / "results/p8_session_b_001_database_export.json").write_text(
        json.dumps(observer.redact(rows), indent=2) + "\n", encoding="utf-8")
    if run_id is not None and rows.get("reports"):
        reopened = Database(configuration.database_url)
        try:
            repository = SQLiteReportRepository(reopened, SQLiteClaimRepository(reopened, 1))
            persisted = await repository.get(run_id)
            observer.record["report_validation_after_database_reopen"] = (
                "PASSED" if persisted is not None else "NO_REPORT")
        except Exception as error:
            observer.record["report_validation_after_database_reopen"] = {
                "error_type": type(error).__name__, "error_code": getattr(error, "code", "unknown")}
        finally:
            await reopened.aclose()
    observer.save()
    print(json.dumps({key: observer.record.get(key) for key in (
        "status", "run_id", "workflow_status", "execution_status", "counts",
        "requested_output_tokens", "stop_reason", "driver_error",
        "total_driver_duration_seconds", "private_env_unchanged",
        "original_sources_unchanged", "database_integrity",
        "report_validation_after_database_reopen",
    )}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-session-b-001", action="store_true", required=True)
    parser.parse_args()
    try:
        asyncio.run(run())
    except Exception as error:
        # No exception text, repr, traceback or settings dumps can expose credentials.
        print(json.dumps({"session_driver_stopped": type(error).__name__,
                          "no_replay_authorized": True}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
