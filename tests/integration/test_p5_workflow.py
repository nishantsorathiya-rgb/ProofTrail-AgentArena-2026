import asyncio
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text
from test_health import api_client
from test_research_api import configured

from app.domain.claims import Verdict
from app.domain.reports import FinalReport, ReportFinding, SynthesisSection
from app.domain.workflow import WorkflowError
from app.infrastructure.database import Database
from app.infrastructure.evidence_models import SourceRow
from app.infrastructure.report_models import ReportRow, RetryRow
from app.infrastructure.workflow_models import AuditRow
from app.main import create_app
from app.workflow import WorkflowRuntime
from scripts.offline_p5 import (
    EMPLOYEES,
    INITIAL_QUERY,
    INJECTION,
    REVENUE,
    OfflineP5Providers,
    public_resolver,
)


@asynccontextmanager
async def scenario(settings, providers=None, **overrides):
    providers = providers or OfflineP5Providers()
    config = configured(settings, **overrides)
    database = Database(config.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        config,
        database,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
    )
    try:
        await runtime.coordinator.start()
        yield runtime, providers, database
    finally:
        await runtime.aclose()
        await database.aclose()


async def execute(runtime):
    submitted = await runtime.coordinator.submit("OFFLINE AlphaCo employees and revenue in 2024")
    await asyncio.wait_for(runtime.coordinator.wait_idle(), 30)
    state = await runtime.coordinator.get(submitted.run_id)
    return (
        state,
        await runtime.coordinator.claims(state.run_id),
        await runtime.coordinator.report(state.run_id),
    )


async def test_actual_p5_graph_researches_new_evidence_reverifies_and_preserves_history(settings):
    async with scenario(settings) as (runtime, providers, database):
        state, claims, report = await execute(runtime)
        assert state.phase == "P5" and state.status == "completed"
        assert state.execution_status == "report_ready" and state.report_id == report.report_id
        assert (
            report.generation_status == "model_organized" and report.completion_status == "partial"
        )
        assert {f.claim_text for f in report.verified_findings} == {EMPLOYEES, REVENUE}
        assert (
            len(report.rejected_claims) == 1
            and "99 people" in report.rejected_claims[0].candidate_text
        )
        assert not any("99 people" in f.claim_text for f in report.executive_summary)
        revenue = next(r for r in claims.claims if r.candidate.claim_text == REVENUE)
        assert [a.result.verdict for a in revenue.attempts] == [
            Verdict.INSUFFICIENT_EVIDENCE,
            Verdict.SUPPORTED,
        ]
        assert len(revenue.candidate.candidate_evidence_ids) == 1
        assert len(revenue.attempts[-1].evidence_scope_ids) == 2
        assert set(revenue.candidate.candidate_evidence_ids).issubset(
            revenue.attempts[-1].evidence_scope_ids
        )
        assert revenue.attempts[-1].result.previous_verdict == "INSUFFICIENT_EVIDENCE"
        retry = state.retry_history[0]
        assert retry.status == "completed" and retry.new_evidence_ids
        assert revenue.attempts[-1].research_retry_id == retry.retry_id
        assert retry.previous_verdicts[str(revenue.candidate.claim_id)] == "INSUFFICIENT_EVIDENCE"
        assert retry.updated_verdicts[str(revenue.candidate.claim_id)] == "SUPPORTED"
        contradicted = next(c for c in claims.claims if "99 people" in c.candidate.claim_text)
        assert (
            len(contradicted.attempts) == 1
        )  # Never search again to find agreement with the false count.
        assert providers.calls == {
            "planner": 1,
            "drafter": 1,
            "verifier": 8,
            "followup": 1,
            "synthesizer": 1,
            "search": 2,
            "extract": 2,
        }
        assert len(set(providers.queries)) == 2 and providers.queries[0] == INITIAL_QUERY
        assert state.budgets.llm_calls == 12 and state.budgets.search_requests == 2
        assert state.verification_llm_requests == 8 and state.verification_prompt_tokens == 160
        assert state.verification_completion_tokens == 80
        assert state.retry_duration_seconds > 0 and state.synthesis_duration_seconds > 0
        assert state.total_duration_seconds >= state.verification_duration_seconds
        await runtime.reports.validate(report)
        async with database.sessions() as session:
            assert len((await session.scalars(select(RetryRow))).all()) == 1
            assert (await session.get(ReportRow, str(state.run_id))).report_id == str(
                report.report_id
            )
            assert len((await session.scalars(select(AuditRow))).all()) == len(state.audit_events)
        events = [e.event for e in state.audit_events]
        assert events.count("verification_decision") == 2
        assert events.index("research_retry_planned") < events.index("retry_research_started")
        assert events.index("retry_research_completed") < events.index("retry_reverified")
        assert events.index("retry_reverified") < events.index("synthesis_completed")
        assert events.index("synthesis_completed") < events.index("report_validated")
        assert [e.sequence for e in state.audit_events] == list(range(1, len(events) + 1))


async def test_supported_only_research_can_finish_complete_after_a_real_retry(settings):
    providers = OfflineP5Providers()
    providers.include_contradiction = False
    async with scenario(settings, providers) as (runtime, _, _):
        state, _, report = await execute(runtime)
        assert report.completion_status == "complete" and state.report_generation_status == "ready"
        assert not report.unresolved_research_gaps and not report.rejected_claims
        assert len(state.retry_history) == 1 and len(report.verified_findings) == 2


async def test_still_insufficient_after_two_retries_terminates_without_fake_promotion(settings):
    providers = OfflineP5Providers()
    providers.retry_result = "unresolved"
    # Two full retries plus independent support reviews need more than the default 12 calls.
    async with scenario(settings, providers, max_llm_calls_per_run=18) as (runtime, _, _):
        state, claims, report = await execute(runtime)
        revenue = next(c for c in claims.claims if c.candidate.claim_text == REVENUE)
        assert len(state.retry_history) == 2 and list(state.retry_counts.values()) == [2]
        assert all(a.result.verdict == Verdict.INSUFFICIENT_EVIDENCE for a in revenue.attempts)
        assert len(revenue.attempts) == 3 and len(revenue.attempts[-1].evidence_scope_ids) == 3
        assert providers.calls["search"] == providers.calls["extract"] == 3
        assert len(set(providers.queries)) == 3
        assert state.budgets.llm_calls == 15
        assert any(g.stop_reason == "retry_limit_reached" for g in report.unresolved_research_gaps)
        assert REVENUE not in {f.claim_text for f in report.verified_findings}


@pytest.mark.parametrize("cap", [0, 1])
async def test_configured_retry_limit_is_persisted_and_enforced(settings, cap):
    providers = OfflineP5Providers()
    providers.retry_result = "unresolved"
    async with scenario(settings, providers, max_retries_per_subquestion=cap) as (runtime, _, _):
        state, _, report = await execute(runtime)
        assert len(state.retry_history) == cap and providers.calls["followup"] == cap
        assert state.budgets.max_retries_per_subquestion == cap
        assert any(g.stop_reason == "retry_limit_reached" for g in report.unresolved_research_gaps)


@pytest.mark.parametrize(
    "mode", ["duplicate_url", "duplicate_content", "empty", "failed", "timeout"]
)
async def test_failed_or_duplicate_retry_does_not_invoke_fake_reverification(settings, mode):
    providers = OfflineP5Providers()
    providers.retry_result = mode
    async with scenario(settings, providers) as (runtime, _, _):
        state, claims, report = await execute(runtime)
        assert len(state.retry_history) == 2
        assert not any(r.new_evidence_ids for r in state.retry_history)
        assert providers.calls["verifier"] == 4  # Original verification plus its support review.
        revenue = next(c for c in claims.claims if c.candidate.claim_text == REVENUE)
        assert len(revenue.attempts) == 1 and revenue.verification_status == "INSUFFICIENT_EVIDENCE"
        assert REVENUE not in {f.claim_text for f in report.verified_findings}
        assert report.completion_status != "complete"
        if mode == "duplicate_url":
            assert state.cache_hits == 2 and providers.calls["extract"] == 1
        if mode in {"failed", "timeout"}:
            assert report.operational_errors and all(
                r.status == "failed" for r in state.retry_history
            )


@pytest.mark.parametrize(
    "limits,reason",
    [
        ({"max_llm_calls_per_run": 5}, "model_budget_exhausted"),
        ({"max_searches_per_run": 1}, "search_budget_exhausted"),
        ({"max_fetches_per_run": 1}, "fetch_budget_exhausted"),
        ({"max_research_tasks_per_run": 1}, "research_task_budget_exhausted"),
    ],
)
async def test_overall_and_request_budgets_stop_research_before_any_extra_request(
    settings, limits, reason
):
    async with scenario(settings, **limits) as (runtime, providers, _):
        state, _, report = await execute(runtime)
        assert not state.retry_history and providers.calls["search"] == 1
        assert any(g.stop_reason == reason for g in report.unresolved_research_gaps)
        assert report.completion_status == "budget_exhausted"
        assert state.budgets.llm_calls <= state.budgets.max_llm_calls
        assert state.budgets.search_requests <= state.budgets.max_searches
        assert state.budgets.fetch_units <= state.budgets.max_fetches
        if "max_llm_calls_per_run" in limits:
            assert providers.calls["synthesizer"] == 0
            assert report.generation_status == "deterministic_incomplete"


@pytest.mark.parametrize("bad", ["duplicate", "unknown_gap", "empty", "malformed"])
async def test_invalid_or_unhelpful_followup_plan_cannot_drive_research(settings, bad):
    providers = OfflineP5Providers()
    providers.bad_followup = bad
    async with scenario(settings, providers) as (runtime, _, _):
        state, _, report = await execute(runtime)
        assert state.correction_stopped and not state.retry_history
        assert providers.calls["followup"] == 1 and providers.calls["search"] == 1
        assert report.completion_status == "partial"
        assert state.correction_stop_reason == (
            "no_useful_followup" if bad == "empty" else "invalid_followup_plan"
        )


async def test_missing_initial_evidence_actually_researches_then_drafts_and_verifies(settings):
    providers = OfflineP5Providers()
    providers.initial_empty = True
    async with scenario(settings, providers) as (runtime, _, _):
        state, claims, report = await execute(runtime)
        assert state.subquestions[0].status == "empty"
        assert providers.calls["search"] == 2 and providers.calls["drafter"] == 1
        assert state.retry_history[0].trigger == "missing_evidence"
        assert state.retry_history[0].drafted_claim_ids and report.verified_findings
        assert all(
            c.attempts[0].research_retry_id == state.retry_history[0].retry_id
            for c in claims.claims
        )


async def test_evidence_without_drafted_claims_is_bounded_incomplete_coverage(settings):
    providers = OfflineP5Providers()
    providers.draft_empty = True
    async with scenario(settings, providers) as (runtime, _, _):
        state, claims, report = await execute(runtime)
        assert len(state.retry_history) == 2 and not claims.claims
        assert providers.calls["verifier"] == providers.calls["synthesizer"] == 0
        assert not report.verified_findings and report.completion_status == "insufficient_evidence"
        assert any(g.kind == "incomplete_coverage" for g in report.unresolved_research_gaps)


async def test_conflict_is_preserved_and_never_retried_to_find_agreement(settings):
    providers = OfflineP5Providers()
    providers.conflicting = True
    async with scenario(settings, providers) as (runtime, _, _):
        state, claims, report = await execute(runtime)
        conflict = next(c for c in claims.claims if c.candidate.claim_text == EMPLOYEES)
        assert conflict.verification_status == "CONFLICTING" and len(conflict.attempts) == 1
        assert all(conflict.candidate.claim_id not in r.claim_ids for r in state.retry_history)
        assert len(report.source_disagreements) == 1
        assert len(report.source_disagreements[0].quotations) == 2
        assert EMPLOYEES not in {f.claim_text for f in report.verified_findings}
        assert report.completion_status == "partial"


@pytest.mark.parametrize("bad", ["citation", "timeout"])
async def test_verification_operational_or_citation_failure_does_not_trigger_confirmation_research(
    settings, bad
):
    providers = OfflineP5Providers()
    providers.bad_verifier = bad
    async with scenario(settings, providers) as (runtime, _, _):
        state, _, report = await execute(runtime)
        assert not state.retry_history and providers.calls["followup"] == 0
        assert any(g.kind == "operational_error" for g in report.unresolved_research_gaps)
        assert REVENUE not in {f.claim_text for f in report.verified_findings}
        assert report.operational_errors


@pytest.mark.parametrize("bad", ["claim_id", "prose", "citation", "timeout"])
async def test_hallucinated_synthesis_or_provider_failure_is_explicit_and_cannot_add_facts(
    settings, bad
):
    providers = OfflineP5Providers()
    providers.bad_synthesis = bad
    async with scenario(settings, providers) as (runtime, _, _):
        state, _, report = await execute(runtime)
        assert report.generation_status == "deterministic_incomplete"
        assert state.report_generation_status == "incomplete"
        assert report.synthesizer_model is None
        assert set(f.claim_text for f in report.verified_findings) == {EMPLOYEES, REVENUE}
        assert "999 people" not in report.model_dump_json()
        assert "fabricated.example.com" not in report.model_dump_json()
        assert (
            "mesh_timeout" in report.operational_errors
            if bad == "timeout"
            else ("invalid_synthesis_output" in report.operational_errors)
        )


async def test_prompt_isolation_and_synthesizer_receives_only_supported_claims(settings):
    async with scenario(settings) as (runtime, providers, _):
        _, _, report = await execute(runtime)
        assert report.mode == "offline_fixture"
        for body in providers.model_payloads:
            assert INJECTION not in body["messages"][0]["content"]
            assert "tools" not in body and "api.openai.com" not in json.dumps(body)
        synthesis = next(
            b
            for b in providers.model_payloads
            if "distinct Synthesizer" in b["messages"][0]["content"]
        )
        payload = json.loads(synthesis["messages"][1]["content"])
        assert {c["claim_text"] for c in payload["UNTRUSTED_DATA"]["supported_claims"]} == {
            EMPLOYEES,
            REVENUE,
        }
        assert "99 people" not in synthesis["messages"][1]["content"]


@pytest.mark.parametrize("tamper", ["url", "quote", "hash", "evidence", "attempt", "run"])
async def test_final_citation_gate_rejects_fabricated_or_changed_reference(settings, tamper):
    async with scenario(settings) as (runtime, _, _):
        state, _, report = await execute(runtime)
        original = report.supporting_citations[0]
        update = {
            "url": {"original_url": "https://fabricated.example.com/never-fetched"},
            "quote": {"text": "Invented supporting quotation"},
            "hash": {"source_content_sha256": "0" * 64},
            "evidence": {"evidence_id": uuid4()},
            "attempt": {"verification_attempt_id": uuid4()},
        }.get(tamper)
        corrupted = (
            report.model_copy(update={"run_id": uuid4()})
            if tamper == "run"
            else report.model_copy(
                update={
                    "supporting_citations": (
                        original.model_copy(update=update),
                        *report.supporting_citations[1:],
                    )
                }
            )
        )
        with pytest.raises(WorkflowError):
            await runtime.reports.validate(corrupted)
        assert await runtime.reports.get(state.run_id) == report


async def test_a_matching_quote_cannot_promote_a_contradicted_claim_into_report(settings):
    async with scenario(settings) as (runtime, _, _):
        _, claims, report = await execute(runtime)
        rejected = next(c for c in claims.claims if c.verification_status == "CONTRADICTED")
        citation = report.supporting_citations[0].model_copy(
            update={"claim_id": rejected.candidate.claim_id}
        )
        finding = ReportFinding(
            claim_id=rejected.candidate.claim_id,
            claim_text=rejected.candidate.claim_text,
            citation_ids=(citation.citation_id,),
        )
        forged = report.model_copy(
            update={
                "verified_findings": (finding,),
                "executive_summary": (finding,),
                "sections": (
                    SynthesisSection(heading="Key findings", claim_ids=(finding.claim_id,)),
                ),
                "supporting_citations": (citation,),
                "rejected_claims": (),
            }
        )
        with pytest.raises(WorkflowError, match="report_claim_not_supported"):
            await runtime.reports.validate(FinalReport.model_validate(forged.model_dump()))


async def test_report_and_retry_history_survive_reopen_and_reports_are_immutable(settings):
    async with scenario(settings) as (runtime, _, database):
        state, _, report = await execute(runtime)
        from app.infrastructure.claim_repository import SQLiteClaimRepository
        from app.infrastructure.report_repository import SQLiteReportRepository
        from app.infrastructure.workflow_repository import SQLiteWorkflowRepository

        reopened = Database(settings.database_url)
        try:
            await reopened.initialize()
            restored = await SQLiteWorkflowRepository(reopened).get(state.run_id)
            assert restored.retry_history == state.retry_history
            reports = SQLiteReportRepository(
                reopened, SQLiteClaimRepository(reopened, max_attempts=3)
            )
            assert await reports.get(state.run_id) == report
            with pytest.raises(WorkflowError, match="report_history_rewrite_rejected"):
                await reports.save(report.model_copy(update={"report_id": uuid4()}))
            async with database.engine.connect() as connection:
                assert (
                    await connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == "0005"
                )
                assert (await connection.execute(text("PRAGMA foreign_key_check"))).all() == []
        finally:
            await reopened.aclose()


async def test_retry_bounds_and_original_plan_are_enforced_by_persistence(settings):
    providers = OfflineP5Providers()
    providers.retry_result = "unresolved"
    async with scenario(settings, providers) as (runtime, _, _):
        state, _, _ = await execute(runtime)
        old = state.retry_history[0]
        changed = old.model_copy(update={"search_query": "Rewritten historical query"})
        with pytest.raises(WorkflowError, match="retry_history_rewrite_rejected"):
            await runtime.store.save(
                state.evolve(retry_history=(changed, *state.retry_history[1:])),
                agent="graph",
                event="illegal_history_rewrite",
            )
        last = state.retry_history[-1]
        third = last.model_copy(
            update={
                "retry_id": uuid4(),
                "number": 3,
                "task_id": uuid4(),
                "search_query": "Forbidden third targeted query",
            }
        )
        with pytest.raises(WorkflowError, match="retry_limit_or_duplicate_query"):
            await runtime.store.save(
                state.evolve(retry_history=(*state.retry_history, third)),
                agent="graph",
                event="illegal_third_retry",
            )
        assert (await runtime.store.get(state.run_id)).retry_history == state.retry_history


async def test_api_p5_admission_audit_retry_json_and_plain_markdown_report(settings):
    providers = OfflineP5Providers()
    providers.planner_release = asyncio.Event()
    application = create_app(
        configured(settings),
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
    )
    async with api_client(application) as client:
        response = await client.post("/research", json={"question": "OFFLINE AlphaCo question"})
        assert response.status_code == 202 and response.json()["phase"] == "P5"
        run_id = response.json()["run_id"]
        UUID(run_id)
        await asyncio.wait_for(providers.planner_entered.wait(), 5)
        pending = await client.get(f"/research/{run_id}/report")
        assert pending.status_code == 202 and pending.json()["report"] is None
        assert "pending_report_json" not in (await client.get(f"/research/{run_id}")).json()
        providers.planner_release.set()
        await application.state.coordinator.wait_idle()
        report = (await client.get(f"/research/{run_id}/report")).json()
        assert (
            len(report["verified_findings"]) == 2
            and report["citation_validation_status"] == "valid"
        )
        retries = (await client.get(f"/research/{run_id}/retries")).json()
        assert len(retries["retries"]) == 1
        audit = (await client.get(f"/research/{run_id}/audit")).json()
        assert audit["events"][-1]["event"] == "workflow_finished"
        markdown = await client.get(f"/research/{run_id}/report?format=markdown")
        assert markdown.status_code == 200 and markdown.headers["content-type"].startswith(
            "text/plain"
        )
        findings = markdown.text.split("## Verified findings")[1].split("## Supporting citations")[
            0
        ]
        assert "99 people" not in findings and "42 people" in findings
        for route in ("report", "retries", "audit"):
            assert (await client.get(f"/research/{uuid4()}/{route}")).status_code == 404
        assert (await client.get(f"/research/{run_id}/report?format=html")).status_code == 422


async def test_api_refuses_a_previously_generated_report_after_source_corruption(settings):
    providers = OfflineP5Providers()
    async with scenario(settings, providers) as (runtime, _, database):
        state, _, report = await execute(runtime)
        async with database.sessions.begin() as session:
            source = await session.get(SourceRow, str(report.supporting_citations[0].source_id))
            source.content = source.content.replace("42", "77")
        with pytest.raises(WorkflowError, match="report_integrity_failed"):
            await runtime.coordinator.report(state.run_id)


async def test_workflow_deadline_leaves_no_report_claiming_success(settings):
    providers = OfflineP5Providers()
    providers.planner_release = asyncio.Event()
    async with scenario(settings, providers, workflow_timeout_seconds=0.05) as (runtime, _, _):
        submitted = await runtime.coordinator.submit("Timeout fixture")
        await asyncio.wait_for(runtime.coordinator.wait_idle(), 5)
        state = await runtime.coordinator.get(submitted.run_id)
        assert state.status == "failed" and "workflow_timeout" in state.errors
        assert state.report_generation_status == "failed" and not state.report_id
        assert await runtime.coordinator.report(state.run_id) is None
        assert providers.calls["search"] == 0


async def test_shutdown_marks_a_real_queued_retry_interrupted_and_never_returns_a_final_report(
    settings,
):
    providers = OfflineP5Providers()
    providers.followup_release = asyncio.Event()
    async with scenario(settings, providers) as (runtime, _, _):
        submitted = await runtime.coordinator.submit("Interrupted correction planning")
        await asyncio.wait_for(providers.followup_entered.wait(), 10)
        await runtime.coordinator.aclose()
        state = await runtime.coordinator.get(submitted.run_id)
        assert state.status == "interrupted" and state.report_generation_status == "failed"
        assert await runtime.coordinator.report(state.run_id) is None
