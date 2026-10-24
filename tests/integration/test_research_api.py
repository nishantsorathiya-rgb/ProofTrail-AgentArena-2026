import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from test_health import api_client
from test_workflow import PLAN, OfflineProviders, execute, public_resolver

from app.application.config import Settings
from app.infrastructure.database import Database
from app.main import create_app
from app.workflow import WorkflowRuntime


def configured(settings, **changes):
    return Settings(
        _env_file=None,
        **{
            **settings.model_dump(),
            "mesh_api_key": "offline-model-key",
            "mesh_model_id": "fixture-model",
            "tavily_api_key": "offline-retrieval-key",
            "tavily_max_attempts": 1,
            **changes,
        },
    )


async def test_post_returns_202_without_waiting_for_planner_and_status_evidence_are_available(
    settings,
):
    providers = OfflineProviders()
    providers.planner_release = asyncio.Event()
    application = create_app(
        configured(settings),
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    async with api_client(application) as client:
        submitted = await client.post(
            "/research", json={"question": "Owner's offline research question"}
        )
        assert submitted.status_code == 202
        run_id = submitted.json()["run_id"]
        UUID(run_id)
        assert submitted.json()["verification_status"] == "pending"
        await asyncio.wait_for(providers.planner_entered.wait(), timeout=3)
        pending = await client.get(f"/research/{run_id}")
        assert pending.json()["execution_status"] == "planning"
        assert pending.headers["x-request-id"]
        providers.planner_release.set()
        await application.state.coordinator.wait_idle()
        status = (await client.get(f"/research/{run_id}")).json()
        assert status["execution_status"] == "verification_completed"
        assert status["budgets"]["llm_calls"] == 8
        evidence = (await client.get(f"/research/{run_id}/evidence")).json()
        assert len(evidence["sources"]) == len(evidence["passages"]) == 3
        assert evidence["trust_level"] == "untrusted_source_text"
        assert "verified_report" not in status
        assert (await client.get(f"/research/{uuid4()}")).status_code == 404
        assert (await client.get(f"/research/{uuid4()}/evidence")).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"question": " "},
        {"question": "x" * 4001},
        {"question": "x", "enable_live_requests": True},
        {},
    ],
)
async def test_invalid_submission_is_rejected_before_model_use(settings, payload):
    providers = OfflineProviders()
    application = create_app(
        configured(settings),
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    async with api_client(application) as client:
        assert (await client.post("/research", json=payload)).status_code == 422
        assert not providers.mesh_calls and not providers.search_calls


async def test_queue_capacity_is_bounded_and_rejection_does_not_create_extra_run(settings):
    providers = OfflineProviders()
    providers.planner_release = asyncio.Event()
    application = create_app(
        configured(settings, max_active_runs=1, max_pending_runs=0),
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    async with api_client(application) as client:
        first = await client.post("/research", json={"question": "First offline question"})
        assert first.status_code == 202
        await providers.planner_entered.wait()
        assert (await client.post("/research", json={"question": "Second"})).status_code == 503
        providers.planner_release.set()
        await application.state.coordinator.wait_idle()
        assert len(providers.mesh_calls) == 8


async def test_offline_default_fails_honestly_with_zero_provider_calls(settings):
    application = create_app(
        settings,
        phase="P4",
    )
    async with api_client(application) as client:
        submitted = await client.post("/research", json={"question": "No live calls authorized"})
        await application.state.coordinator.wait_idle()
        result = (await client.get(f"/research/{submitted.json()['run_id']}")).json()
        assert result["status"] == "failed" and result["errors"] == ["live_requests_disabled"]
        assert result["budgets"]["llm_calls"] == result["budgets"]["search_requests"] == 0


async def test_request_budget_exhaustion_is_terminal_and_cannot_bypass_caps(settings):
    providers = OfflineProviders()
    providers.plan = {"goal": "Budget test", "subquestions": PLAN["subquestions"][:1]}
    providers.behavior = {"alpha": "quota"}
    config = configured(settings, max_searches_per_run=1, tavily_max_attempts=3)
    database = Database(config.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        config,
        database,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    try:
        state = await execute(runtime)
        assert state.execution_status == "budget_exhausted"
        assert state.budgets.search_requests == len(providers.search_calls) == 1
        assert state.budgets.extract_requests == 0
        assert state.subquestions[0].status == "budget_exhausted"
    finally:
        await runtime.aclose()
        await database.aclose()


async def test_configurable_task_concurrency_two_never_runs_three_requests(settings):
    providers = OfflineProviders()
    providers.overlap_target = 2
    config = configured(settings, max_parallel_research_tasks=2)
    database = Database(config.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        config,
        database,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    try:
        state = await execute(runtime)
        assert state.execution_status == "verification_completed" and providers.peak == 2
    finally:
        await runtime.aclose()
        await database.aclose()


async def test_workflow_deadline_cancels_real_work_and_saves_failure(settings):
    providers = OfflineProviders()
    providers.planner_release = asyncio.Event()
    config = configured(settings, workflow_timeout_seconds=0.2)
    application = create_app(
        config,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    async with api_client(application) as client:
        submitted = await client.post("/research", json={"question": "Deadline fixture"})
        await asyncio.wait_for(application.state.coordinator.wait_idle(), timeout=5)
        result = (await client.get(f"/research/{submitted.json()['run_id']}")).json()
        assert result["status"] == "failed" and "workflow_timeout" in result["errors"]
        assert not providers.search_calls


async def test_planner_limit_rejects_too_many_unique_tasks(settings):
    providers = OfflineProviders()
    config = configured(settings, max_research_subquestions=1)
    application = create_app(
        config,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P4",
    )
    async with api_client(application) as client:
        submitted = await client.post("/research", json={"question": "One task only"})
        await application.state.coordinator.wait_idle()
        result = (await client.get(f"/research/{submitted.json()['run_id']}")).json()
        assert result["errors"] == ["invalid_planner_response"] and not providers.search_calls
