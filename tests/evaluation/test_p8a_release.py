"""Revalidate stored reports after provenance corruption, not just agent output."""

import asyncio
import json
import subprocess

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import delete

from app.domain.claims import VerificationResult
from app.domain.workflow import WorkflowError
from app.evaluation.harness import fixture_settings
from app.infrastructure.claim_models import VerificationRow
from app.infrastructure.database import Database
from app.infrastructure.workflow_models import AuditRow, ModelUsageRow
from app.workflow import WorkflowRuntime
from scripts.offline_p5 import OfflineP5Providers, public_resolver
from scripts.p8a_live_preflight import plan
from scripts.p8a_security import scan, scan_text


@pytest.fixture
async def completed_run(settings):
    config = fixture_settings(
        database_url=settings.database_url,
        mesh_api_key="offline-key",
        mesh_model_id="offline-mesh-p5-fixture",
        tavily_api_key="offline-key",
        tavily_max_attempts=1,
    )
    providers = OfflineP5Providers()
    database = Database(config.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        config, database,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
    )
    try:
        await runtime.coordinator.start()
        state = await runtime.coordinator.submit("OFFLINE AlphaCo employees and revenue 2024")
        await asyncio.wait_for(runtime.coordinator.wait_idle(), 30)
        claims = await runtime.coordinator.claims(state.run_id)
        report = await runtime.coordinator.report(state.run_id)
        yield runtime, database, state, claims, report
    finally:
        await runtime.aclose()
        await database.aclose()


@pytest.mark.parametrize("corruption", ["missing_receipt", "failed_usage", "missing_audit"])
async def test_saved_verified_report_is_withheld_after_support_provenance_changes(
    completed_run, corruption
):
    runtime, database, state, _, report = completed_run
    assert report.verified_findings
    citation = report.supporting_citations[0]
    async with database.sessions.begin() as session:
        attempt = await session.get(VerificationRow, str(citation.verification_attempt_id))
        raw = json.loads(attempt.result_json)
        if corruption == "missing_receipt":
            raw.pop("support_review_receipt")
            attempt.result_json = json.dumps(raw)
        elif corruption == "failed_usage":
            usage = await session.get(
                ModelUsageRow, raw["support_review_receipt"]["review_usage_id"]
            )
            usage.status = "error"
        else:
            await session.execute(
                delete(AuditRow).where(
                    AuditRow.run_id == str(state.run_id),
                    AuditRow.event == "support_review_completed",
                )
            )
    with pytest.raises(WorkflowError, match="report_integrity_failed"):
        await runtime.reports.get(state.run_id)
    with pytest.raises(WorkflowError, match="semantic_support_review"):
        await runtime.reports.validate(report)


@pytest.mark.parametrize("outcome", ["operational_error", "validation_rejected"])
async def test_failure_cannot_be_deserialized_as_supported(completed_run, outcome):
    _, _, _, claims, _ = completed_run
    result = next(c for c in claims.claims if c.verification_status == "SUPPORTED")
    raw = result.attempts[-1].result.model_dump(mode="json")
    raw.update(outcome=outcome, rejection_reason="mesh_timeout")
    with pytest.raises(ValidationError, match="Failures cannot certify"):
        VerificationResult.model_validate(raw)


def test_release_scan_reports_location_without_credentials_and_preflight_cannot_spend():
    token = "rsk_" + "A7b9C2d4E6f8G0h1" * 2
    findings = scan_text("secret=" + token)
    assert findings == [{"line": 1, "rule": "mesh_key"}]
    assert token not in json.dumps(findings)
    assert not scan_text("MESH_API_KEY='YOUR_MESH_KEY_HERE'")
    proposed = plan()
    assert proposed["currently_authorized_spending_cap_usd"] == 0
    assert proposed["ready_for_live_execution"] is False
    assert proposed["estimated_maximum_total_usd"] is None


def test_candidate_scan_includes_untracked_files_and_respects_ignored_private_files(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".env\n.env.*\ndata/\n.venv-*/\n*.key\n")
    credential = "tvly-" + "Z1a2B3c4D5e6F7g8" * 2
    (tmp_path / "candidate.py").write_text("key='" + credential + "'\n")
    (tmp_path / ".env").write_text("TAVILY_API_KEY=" + credential)
    result = scan(tmp_path)
    assert result["passed"] is False
    assert result["findings"] == [{"path": "candidate.py", "line": 1, "rule": "tavily_key"}]
    assert len(result["ignore_probes"]) == 5
    assert credential not in json.dumps(result)
