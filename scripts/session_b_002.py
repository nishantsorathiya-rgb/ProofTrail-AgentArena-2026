"""B002 preparation/checks are offline; live execution requires separate owner approval.

Never rerun a reserved session. This launcher calls the existing FastAPI/LangGraph
composition and does not change any agent, semantic gate or provider adapter.
"""

import argparse
import asyncio
import json
import math
import re
import sqlite3
import subprocess
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.agents.drafter import SYSTEM_PROMPT as DRAFT_PROMPT
from app.agents.followup_planner import SYSTEM_PROMPT as FOLLOWUP_PROMPT
from app.agents.planner import SYSTEM_PROMPT as PLAN_PROMPT
from app.agents.synthesizer import SYSTEM_PROMPT as SYNTHESIS_PROMPT
from app.agents.verifier import SUPPORT_REVIEW_PROMPT
from app.agents.verifier import SYSTEM_PROMPT as VERIFY_PROMPT
from app.application.config import Settings
from app.application.reports import markdown_report
from app.infrastructure.claim_repository import SQLiteClaimRepository
from app.infrastructure.database import Database
from app.infrastructure.report_repository import SQLiteReportRepository
from app.main import create_app
from scripts.session_b_live import QUESTION, SessionObserver, digest, now

ROOT = Path(__file__).resolve().parents[1]
STEM = "p8_session_b_002"
JOURNAL = ROOT / f"results/{STEM}_execution.json"
DATABASE = ROOT / f"data/{STEM}.db"
MANIFEST = ROOT / "results/p8d_source_manifest.json"
EXPECTED = {
    "mesh_base_url": "https://api.meshapi.ai/v1", "mesh_model_id": "openai/gpt-4.1-mini",
    "enable_live_requests": False, "max_llm_calls_per_run": 8,
    "max_searches_per_run": 2, "max_fetches_per_run": 2, "max_claims_per_run": 1,
    "max_research_subquestions": 1, "max_research_tasks_per_run": 2,
    "max_retries_per_subquestion": 1, "max_parallel_fetches": 1,
    "max_parallel_research_tasks": 1, "max_parallel_verifications": 1,
    "max_active_runs": 1, "max_pending_runs": 0, "tavily_max_attempts": 1,
    "http_timeout_seconds": 12, "workflow_timeout_seconds": 120,
    "max_source_text_chars": 12000, "max_draft_input_chars": 24000,
    "max_verification_input_chars": 12000, "fetch_max_bytes": 1500000,
    "page_cache_ttl_seconds": 3600, "dns_timeout_seconds": 3,
}
ROLES = {PLAN_PROMPT: "planner", DRAFT_PROMPT: "drafter", VERIFY_PROMPT: "primary",
         SUPPORT_REVIEW_PROMPT: "blind_review", SYNTHESIS_PROMPT: "synthesizer",
         FOLLOWUP_PROMPT: "followup_planner"}


def verify_manifest():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["sha256"]
    current = {p.relative_to(ROOT).as_posix() for folder in ("app", "scripts", "tests")
               for p in (ROOT / folder).rglob("*.py") if "__pycache__" not in p.parts}
    if current != {name for name in expected if name.endswith(".py")}:
        raise ValueError("reviewed_source_file_set_changed")
    for name, value in expected.items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT.resolve()) or digest(path) != value:
            raise ValueError("reviewed_source_changed")
    return digest(MANIFEST)


def require_fresh():
    if any((ROOT / "results").glob(f"{STEM}_*")) or any(
        (ROOT / "data").glob(f"{STEM}.db*")
    ):
        raise ValueError("session_reserved_or_artifacts_exist_do_not_replay")


def preflight():
    settings = Settings()
    if any(getattr(settings, name) != value for name, value in EXPECTED.items()):
        raise ValueError("local_settings_differ_from_reviewed_envelope")
    for secret, pattern in ((settings.mesh_api_key, r"rsk_[A-Za-z0-9_-]{16,512}"),
                            (settings.tavily_api_key, r"tvly-[A-Za-z0-9_-]{16,512}")):
        if secret is None or not re.fullmatch(pattern, secret.get_secret_value()):
            raise ValueError("private_key_missing_or_invalid_format")
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT,
                             capture_output=True).returncode
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=ROOT,
                             capture_output=True).returncode
    if ignored != 0 or tracked != 1:
        raise ValueError("private_env_not_ignored_and_untracked")
    require_fresh()
    return settings, verify_manifest()


class RetestObserver(SessionObserver):
    """Reuse B001's tested counters/redaction, never its preflight/run/journal."""

    def __init__(self, secrets):
        super().__init__(secrets)
        self.record.update(session="P8 Session B 002", owner_approval=
                           "Separate B002 single-run approval required; CLI flag alone is not "
                           "evidence that the owner has granted permission.")

    def save(self):
        self.capture_consumed_mesh_responses()
        JOURNAL.write_text(json.dumps(self.redact(self.record), indent=2) + "\n", encoding="utf-8")

    async def before(self, request):
        try:
            if (request.method != "POST" or request.url.scheme != "https"
                    or request.url.port not in {None, 443} or request.url.query):
                raise RuntimeError("unapproved_provider_endpoint")
            payload = json.loads(request.content)
            role = None
            if request.url.host == "api.meshapi.ai":
                role = ROLES.get(payload.get("messages", [{}])[0].get("content"))
                if role is None or payload.get("stream", False):
                    raise RuntimeError("unapproved_mesh_role_or_stream")
                expected_output = 4096 if role == "drafter" else 2048
                if payload.get("max_tokens") != expected_output:
                    raise RuntimeError("unapproved_role_output_limit")
                if role in {"primary", "blind_review"}:
                    data = json.loads(payload["messages"][1]["content"])
                    version = data["output_schema"]["properties"]["contract_version"]["const"]
                    if version != "exact_quotes_v2":
                        raise RuntimeError("unapproved_verification_contract")
            await super().before(request)
            self.record["provider_requests"][-1]["role"] = role
            self.save()
        except Exception:
            self.fatal.set()
            raise

    async def after(self, response):
        await super().after(response)
        if response.status_code != 200:
            self.record["stop_reason"] = "provider_http_error_no_further_attempt"
            self.fatal.set()
            self.save()


class StopOnStreamError(httpx.AsyncByteStream):
    def __init__(self, inner, observer):
        self.inner, self.observer = inner, observer

    async def __aiter__(self):
        try:
            async for chunk in self.inner:
                yield chunk
        except Exception as error:
            self.observer.record["stop_reason"] = "provider_body_error_no_further_attempt"
            self.observer.record["transport_error_type"] = type(error).__name__
            self.observer.fatal.set()
            self.observer.save()
            raise

    async def aclose(self):
        await self.inner.aclose()


class StopOnTransportError(httpx.AsyncBaseTransport):
    """Stop the session on transport failure, without retransmission or error text."""

    def __init__(self, inner, observer):
        self.inner, self.observer = inner, observer

    async def handle_async_request(self, request):
        try:
            response = await self.inner.handle_async_request(request)
            response.stream = StopOnStreamError(response.stream, self.observer)
            return response
        except Exception as error:
            self.observer.record["stop_reason"] = "provider_transport_error_no_further_attempt"
            self.observer.record["transport_error_type"] = type(error).__name__
            self.observer.fatal.set()
            self.observer.save()
            raise

    async def aclose(self):
        await self.inner.aclose()


def export_json(suffix, value, observer):
    with (ROOT / f"results/{STEM}_{suffix}.json").open("x", encoding="utf-8") as stream:
        json.dump(observer.redact(value), stream, indent=2)
        stream.write("\n")


def sql_export():
    with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        names = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        rows = {name: [dict(r) for r in connection.execute(f'SELECT * FROM "{name}"')]
                for name in names}
        integrity = {"integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
                     "foreign_key_violations": [tuple(r) for r in connection.execute(
                         "PRAGMA foreign_key_check")],
                     "alembic_head": (rows.get("alembic_version") or [{}])[0].get("version_num")}
    return rows, integrity


async def reopened_report(configuration, run_id, rows):
    # The real table is final_reports; do not repeat B001's reports-table guard bug.
    if run_id is None or not rows.get("final_reports"):
        return "NO_REPORT"
    database = Database(configuration.database_url)
    try:
        reports = SQLiteReportRepository(database, SQLiteClaimRepository(database, 1))
        report = await reports.get(run_id)  # Revalidates current evidence and semantic receipts.
        return "PASSED" if report is not None else "NO_REPORT"
    finally:
        await database.aclose()


def usage_summary(observer, rows):
    mesh = [r for r in observer.record["provider_requests"] if r["operation"] == "mesh"]
    returned = [r.get("response_body", {}).get("usage", {}) for r in mesh]

    def total(name):
        values = [u.get(name) for u in returned]
        return sum(values) if mesh and all(type(v) is int and v >= 0 for v in values) else None

    inputs, outputs = total("prompt_tokens"), total("completion_tokens")
    retrieval = rows.get("retrieval_usage", [])
    credits = [r.get("reported_credits") for r in retrieval]
    return {"mesh_completions_attempted": len(mesh), "input_tokens": inputs,
        "output_tokens": outputs,
        "requested_output_tokens": observer.record["requested_output_tokens"],
        "usage_based_mesh_cost_estimate_usd": round(
            (inputs * 0.40 + outputs * 1.60) / 1000000, 10
        ) if inputs is not None and outputs is not None else None,
        "pricing_basis": "Saved 18 October catalog: USD 0.40/M input, 1.60/M output; "
        "owner must confirm still applicable before execution, not a fresh API lookup",
        "settled_cost_usd": None,
        "reported_tavily_credits": sum(credits) if credits and all(
            type(v) in {int, float} and math.isfinite(v) and v >= 0 for v in credits) else None,
        "gateway_attempt_count": "UNAVAILABLE_UNLESS_RETURNED_IN_OBSERVED_HEADERS"}


def acceptance(trace, observer, integrity, reopened):
    state, report = trace.get("state", {}), trace.get("report") or {}
    events = state.get("audit_events", [])
    records = trace.get("claims", {}).get("claims", [])
    requests = observer.record["provider_requests"]
    roles = {r.get("role") for r in requests if r.get("status_code") == 200}
    findings = report.get("verified_findings", [])
    supported = set(trace.get("claims", {}).get("supported_claim_ids", []))
    resolutions = [e for e in events if e["event"] == "quotation_resolution_completed"]
    receipts = []
    resolution_bindings = []
    for finding in findings:
        record = next((r for r in records if r["candidate"]["claim_id"] == finding["claim_id"]), {})
        result = ((record.get("attempts") or [{}])[-1].get("result") or {})
        receipt = result.get("support_review_receipt") or {}
        receipts.append(bool(receipt) and result.get("outcome") == "completed"
                        and receipt.get("primary_usage_id") != receipt.get("review_usage_id")
                        and receipt.get("primary_decision", {}).get("verdict") == "SUPPORTED"
                        and receipt.get("review_decision", {}).get("verdict") == "SUPPORTED")
        for role, usage in (("primary", receipt.get("primary_usage_id")),
                            ("blind_review", receipt.get("review_usage_id"))):
            resolution_bindings.append(any(
                e["details"].get("claim_id") == finding["claim_id"]
                and e["details"].get("attempt_id") == result.get("attempt_id")
                and e["details"].get("role") == role
                and e["details"].get("model_usage_id") == usage
                and e["details"].get("evidence_sha256") == receipt.get("evidence_sha256")
                and e["details"].get("contract_version") == "exact_quotes_v2"
                and e["details"].get("quote_count", 0) > 0 for e in resolutions
            ))
    checks = {
        "real_planner": "planner" in roles and bool(state.get("research_plan")),
        "real_retrieval": all(any(r["operation"] == op and r["status_code"] == 200
                                   for r in requests) for op in ("search", "extract"))
        and bool(trace.get("evidence", {}).get("passages")),
        "candidate_persisted": bool(records) and all(
            r["candidate"]["verification_status"] == "UNVERIFIED" for r in records),
        "primary_semantic_call": "primary" in roles,
        "blind_semantic_call": "blind_review" in roles,
        "exact_quotes_v2_both_roles": bool(findings) and all(resolution_bindings),
        "evidence_bound_support_receipts": bool(findings) and all(receipts),
        "model_synthesis": "synthesizer" in roles
        and report.get("generation_status") == "model_organized",
        "complete_persisted_report": bool(findings)
        and report.get("completion_status") == "complete" and reopened == "PASSED",
        "only_supported_findings": bool(findings) and all(
            f["claim_id"] in supported for f in findings),
        "citations_revalidated": bool(report.get("supporting_citations"))
        and trace.get("final_report_repository_validation") == "PASSED" and reopened == "PASSED",
        "official_documentation_cited": bool(report.get("supporting_citations")) and all(
            urlsplit(c["original_url"]).hostname == "docs.python.org"
            for c in report.get("supporting_citations", [])),
        "ordered_audit": bool(events) and [e["sequence"] for e in events]
        == list(range(1, len(events) + 1)),
        "database_integrity": integrity["integrity_check"] == "ok"
        and not integrity["foreign_key_violations"] and integrity["alembic_head"] == "0005",
        "no_provider_stop": not observer.fatal.is_set(),
    }
    return {"checks": checks, "passed": all(checks.values()),
            "semantic_accuracy": "NOT_MEASURED_BY_ONE_RUN",
            "conditional_retry": "EXERCISED" if state.get("retry_history") else "NOT_EXERCISED"}


async def run():
    settings, manifest_hash = preflight()
    env_hash = digest(ROOT / ".env")  # Kept in memory only; never publish secret-derived hashes.
    observer = RetestObserver([settings.mesh_api_key.get_secret_value(),
                               settings.tavily_api_key.get_secret_value()])
    observer.record["reviewed_source_manifest_sha256"] = manifest_hash
    with JOURNAL.open("x", encoding="utf-8") as stream:
        json.dump(observer.record, stream, indent=2)
    with DATABASE.open("xb"):
        pass
    configuration = settings.model_copy(update={"enable_live_requests": True,
        "max_fetches_per_run": 10, "workflow_timeout_seconds": 300,
        "database_url": "sqlite:///" + DATABASE.as_posix()})
    observer.record["process_configuration"] = {
        name: getattr(configuration, name) for name in EXPECTED}
    observer.save()
    started, run_id, trace = perf_counter(), None, {}
    try:
        app = create_app(configuration)  # Actual default P5 graph; no fixture transports/resolver.
        coordinator = app.state.coordinator
        workflow = coordinator._workflow
        mesh = workflow._planner._model._model
        tavily = workflow._researcher._collection._search._client
        sdk = mesh._get_client()  # Construction only, not an authentication request.
        if sdk.max_retries != 0 or mesh._mock_transport is not None or tavily._mock is not None:
            raise RuntimeError("real_adapters_without_retries_required")
        for client in (sdk._client, tavily._get_client()):
            client._transport = StopOnTransportError(client._transport, observer)
            client.event_hooks["request"].append(observer.before)
            client.event_hooks["response"].append(observer.after)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://prooftrail.local") as local:
                accepted = await local.post("/research", json={"question": QUESTION})
                observer.record["submission"] = {"status_code": accepted.status_code,
                    "body": accepted.json(), "research_posts": 1}
                if accepted.status_code != 202:
                    raise RuntimeError("research_submission_not_accepted")
                run_id = UUID(accepted.json()["run_id"])
                observer.record.update(run_id=str(run_id), status="RUNNING")
                observer.save()
                idle = asyncio.create_task(coordinator.wait_idle())
                halt = asyncio.create_task(observer.fatal.wait())
                try:
                    done, _ = await asyncio.wait({idle, halt}, return_when=asyncio.FIRST_COMPLETED)
                    if halt in done and observer.fatal.is_set():
                        await coordinator.aclose()
                    await idle
                finally:
                    halt.cancel()
                    await asyncio.gather(halt, return_exceptions=True)
                state = await coordinator.get(run_id)
                trace = {"graph": workflow.graph.get_graph().to_json(),
                         "state": state.model_dump(mode="json"), "report": None}
                for name in ("claims", "evidence"):
                    try:
                        trace[name] = (await getattr(coordinator, name)(run_id)).model_dump(
                            mode="json")
                    except Exception as error:
                        trace[name] = {}
                        trace[name + "_error"] = {"type": type(error).__name__,
                                                  "code": getattr(error, "code", "unknown")}
                try:
                    report = await coordinator.report(run_id)
                    if report is not None:
                        await coordinator._reports.validate(report)
                        trace["report"] = report.model_dump(mode="json")
                        trace["final_report_repository_validation"] = "PASSED"
                        with (ROOT / f"results/{STEM}_report.md").open("x", encoding="utf-8") as f:
                            f.write(observer.redact(markdown_report(report)))
                except Exception as error:
                    trace["report_error"] = {"type": type(error).__name__,
                                             "code": getattr(error, "code", "unknown")}
    except Exception as error:
        observer.fatal.set()
        observer.record["driver_error"] = {"type": type(error).__name__,
                                           "code": getattr(error, "code", "driver_error")}
    finally:
        observer.record["workflow_and_export_duration_seconds"] = round(perf_counter() - started, 6)
        observer.save()
    try:
        rows, integrity = sql_export()
        export_json("database_export", rows, observer)
        reopened = await reopened_report(configuration, run_id, rows)
        observer.record["database_integrity"] = integrity
        observer.record["report_validation_after_database_reopen"] = reopened
        observer.record["acceptance"] = acceptance(trace, observer, integrity, reopened)
        observer.capture_consumed_mesh_responses()
        observer.record["usage_summary"] = usage_summary(observer, rows)
        observer.record["acceptance"]["checks"]["usage_attempts_reconciled"] = (
            sum(bool(r["outbound_called"]) for r in rows.get("model_usage", []))
            == observer.record["counts"]["mesh"]
            and all(sum(r["operation"] == op for r in rows.get("retrieval_usage", []))
                    == observer.record["counts"][op] for op in ("search", "extract")))
        if len(rows.get("research_runs", [])) != 1:
            observer.record["acceptance"]["passed"] = False
            observer.record["acceptance"]["checks"]["exactly_one_run"] = False
        observer.record["acceptance"]["passed"] = all(
            observer.record["acceptance"]["checks"].values())
    except Exception as error:
        observer.record["postprocessing_error"] = {"type": type(error).__name__,
                                                   "code": getattr(error, "code", "unknown")}
    export_json("trace", trace, observer)
    observer.record.update(status="STOPPED_AFTER_SINGLE_EXECUTION", finished_at_utc=now(),
        total_driver_duration_seconds=round(perf_counter() - started, 6),
        private_env_unchanged=digest(ROOT / ".env") == env_hash)
    try:
        observer.record["reviewed_sources_unchanged"] = verify_manifest() == manifest_hash
    except (ValueError, OSError):
        observer.record["reviewed_sources_unchanged"] = False
    observer.save()
    passed = (observer.record.get("acceptance", {}).get("passed", False)
              and observer.record["private_env_unchanged"]
              and observer.record["reviewed_sources_unchanged"])
    print(json.dumps({"session": "B002", "passed": passed,
        "counts": observer.record["counts"], "journal": JOURNAL.name,
        "no_replay_authorized": True}), flush=True)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Read-only local checks, no API calls")
    action.add_argument("--approved-session-b-002", action="store_true",
                        help="Only after explicit separate owner approval of B002")
    args = parser.parse_args()
    try:
        if args.check:
            _settings, manifest_hash = preflight()
            print(json.dumps({"local_configuration": "READY_FORMAT_ONLY",
                "application_live_enabled": False, "api_requests": 0,
                "fresh_session_artifacts_absent": True, "source_manifest_sha256": manifest_hash,
                "dedicated_key_identity_and_quota": "OWNER_CONFIRMATION_REQUIRED",
                "current_credits_and_price": "OWNER_CONFIRMATION_REQUIRED",
                "execution_authorized": False}, indent=2))
            return 0
        return asyncio.run(run())
    except Exception as error:
        print(json.dumps({"session_stopped": type(error).__name__,
                          "no_replay_authorized": True}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
