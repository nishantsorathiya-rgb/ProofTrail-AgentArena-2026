import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from test_health import api_client
from test_research_api import configured
from test_workflow import execute

from app.domain.workflow import BudgetState, WorkflowState
from app.infrastructure.database import Database
from app.infrastructure.workflow_repository import SQLiteWorkflowRepository
from app.main import create_app
from app.workflow import WorkflowRuntime
from scripts.offline_p4 import OfflineP4Providers, public_resolver


async def test_real_p4_graph_agents_and_api_expose_auditable_claims(settings):
    providers = OfflineP4Providers()
    application = create_app(
        configured(settings),
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    async with api_client(application) as client:
        submitted = await client.post("/research", json={"question": "OFFLINE AlphaCo research"})
        assert submitted.status_code == 202 and submitted.json()["phase"] == "P4"
        run_id = submitted.json()["run_id"]
        await application.state.coordinator.wait_idle()
        state = (await client.get(f"/research/{run_id}")).json()
        claims = (await client.get(f"/research/{run_id}/claims")).json()
        evidence = (await client.get(f"/research/{run_id}/evidence")).json()
        assert state["verification_status"] == "partial" and state["verification_failures"] == 1
        assert state["collection_status"] == "evidence_collected"
        assert state["verification_counts"] == {
            "SUPPORTED": 1,
            "CONTRADICTED": 1,
            "INSUFFICIENT_EVIDENCE": 2,
        }
        assert len(claims["supported_claim_ids"]) == 1 and len(claims["claims"]) == 4
        assert evidence["verification_status"] == "unverified_source_material"
        assert state["budgets"]["llm_calls"] == 7 and state["verification_llm_requests"] == 5
        assert state["verification_prompt_tokens"] == 100
        assert state["verification_completion_tokens"] == 50
        assert state["verification_duration_seconds"] > 0
        assert state["verification_persistence_duration_ms"] > 0
        events = state["audit_events"]
        names = [e["event"] for e in events]
        assert names.index("planner_completed") < names.index("research_completed")
        assert names.index("research_completed") < names.index("draft_completed")
        assert names.index("draft_completed") < names.index("verification_started")
        assert names.index("verification_completed") < names.index("verification_summarized")
        assert {e["agent"] for e in events} >= {"planner", "researcher", "drafter", "verifier"}
        attempt_ids = {r["attempts"][0]["attempt_id"] for r in claims["claims"]}
        recorded = {
            e["details"]["attempt_id"]
            for e in events
            if e["event"] == "verification_attempt_finished"
        }
        assert attempt_ids == recorded
        assert "verified_report" not in state and "report" not in claims
        assert (await client.get(f"/research/{uuid4()}/claims")).status_code == 404


@pytest.mark.parametrize("mode", ["malformed", "evidence_id", "task_id", "limit"])
async def test_invalid_claim_draft_fails_before_any_verifier_call(settings, mode):
    providers = OfflineP4Providers()

    def invalid(output, payload):
        if mode == "malformed":
            return "not JSON"
        if mode == "evidence_id":
            output["claims"][0]["candidate_evidence_ids"] = [str(uuid4())]
        if mode == "task_id":
            output["claims"][0]["originating_research_task_id"] = str(uuid4())
        return output

    providers.draft_override = invalid
    config = configured(settings, max_claims_per_run=3 if mode == "limit" else 18)
    application = create_app(
        config,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    async with api_client(application) as client:
        response = await client.post("/research", json={"question": "Draft safety fixture"})
        await application.state.coordinator.wait_idle()
        state = (await client.get(f"/research/{response.json()['run_id']}")).json()
        assert state["status"] == state["execution_status"] == "failed"
        assert state["verification_status"] == "failed" and "invalid_claim_draft" in state["errors"]
        assert providers.calls["verifier"] == 0 and not state["candidate_claim_ids"]


@pytest.mark.parametrize("empty", [True, False])
async def test_empty_and_duplicate_drafts_have_explicit_outcomes(settings, empty):
    providers = OfflineP4Providers()

    def draft(output, payload):
        output["claims"] = [] if empty else [output["claims"][0]] * 4
        return output

    providers.draft_override = draft
    database = Database(settings.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        configured(settings),
        database,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    try:
        await runtime.coordinator.start()
        state = await execute(runtime)
        if empty:
            assert not state.candidate_claim_ids and state.verification_status == "not_run"
            assert "no_candidate_claims_generated" in state.warnings
            assert providers.calls["verifier"] == 0
        else:
            assert len(state.candidate_claim_ids) == 1 and providers.calls["verifier"] == 2
            assert state.verification_counts == {"SUPPORTED": 1}
    finally:
        await runtime.aclose()
        await database.aclose()


@pytest.mark.parametrize("cap", [1, 3])
async def test_shared_budget_covers_planning_drafting_and_parallel_verification(settings, cap):
    providers = OfflineP4Providers()
    application = create_app(
        configured(settings, max_llm_calls_per_run=cap),
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    async with api_client(application) as client:
        response = await client.post("/research", json={"question": "Global budget fixture"})
        await application.state.coordinator.wait_idle()
        state = (await client.get(f"/research/{response.json()['run_id']}")).json()
        assert state["budgets"]["llm_calls"] == cap
        assert "model_budget_exhausted" in state["errors"]
        if cap == 1:
            assert providers.calls["drafter"] == providers.calls["verifier"] == 0
            assert state["execution_status"] == "budget_exhausted"
        else:
            assert providers.calls["verifier"] == 1
            # Any candidate may win the final slot, including the invalid-citation fixture.
            claims = (await client.get(f"/research/{response.json()['run_id']}/claims")).json()[
                "claims"
            ]
            primary_support = sum(
                bool(record["attempts"][0]["result"]["primary_decision"])
                and record["attempts"][0]["result"]["primary_decision"]["verdict"] == "SUPPORTED"
                for record in claims
            )
            assert (
                sum(
                    record["attempts"][0]["result"]["rejection_reason"] == "model_budget_exhausted"
                    for record in claims
                )
                == 3 + primary_support
            )
            assert not any(record["verification_status"] == "SUPPORTED" for record in claims)
            assert sum(record["attempts"][0]["result"]["llm_requests"] for record in claims) == 1


async def test_shutdown_during_semantic_call_cannot_leave_supported_pending_claims(settings):
    entered, release = asyncio.Event(), asyncio.Event()
    providers = OfflineP4Providers()

    async def blocked(output, payload):
        entered.set()
        await release.wait()
        return output

    providers.verifier_override = blocked
    database = Database(settings.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        configured(settings),
        database,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    try:
        await runtime.coordinator.start()
        submitted = await runtime.coordinator.submit("Cancellation fixture")
        await asyncio.wait_for(entered.wait(), 5)
        await runtime.coordinator.aclose()
        state = await runtime.coordinator.get(submitted.run_id)
        collection = await runtime.claims.collection(submitted.run_id)
        assert state.status == "interrupted" and state.verification_status == "failed"
        assert not collection.supported_claim_ids
        assert all(a.status != "pending" for r in collection.claims for a in r.attempts)
        assert all(
            a.result is None or a.result.outcome == "operational_error"
            for r in collection.claims
            for a in r.attempts
        )
    finally:
        await runtime.aclose()
        await database.aclose()


async def test_0003_upgrade_preserves_legacy_p3_snapshot_and_schema_consistency(settings):
    database = Database(settings.database_url)
    if database._path:
        database._path.parent.mkdir(parents=True, exist_ok=True)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[2] / "app/infrastructure/migrations")
    )

    def to_0003(connection):
        config.attributes["connection"] = connection
        command.upgrade(config, "0003")

    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(to_0003)
        store = SQLiteWorkflowRepository(database)
        legacy = WorkflowState(
            run_id=uuid4(),
            original_question="Legacy P3 question",
            phase="P3",
            mode="offline_fixture",
            budgets=BudgetState(max_llm_calls=12, max_searches=6, max_fetches=10),
        )
        await store.create(legacy)
        before = await store.get(legacy.run_id)
        await database.initialize()
        assert await store.get(legacy.run_id) == before
        async with database.engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
            )
            assert (await connection.execute(text("PRAGMA foreign_key_check"))).all() == []

            def schema_check(sync_connection):
                config.attributes["connection"] = sync_connection
                command.check(config)

            await connection.run_sync(schema_check)
    finally:
        await database.aclose()
