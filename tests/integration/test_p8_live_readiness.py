"""Synthetic offline budget witnesses for the planned one-claim live session.

These exercise actual application nodes, not live semantic model accuracy.
"""

import json

import httpx
import pytest

from app.infrastructure.database import Database
from app.workflow import WorkflowRuntime
from scripts.offline_p5 import EMPLOYEES, REVENUE, OfflineP5Providers, public_resolver


class OneClaimProviders(OfflineP5Providers):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.blind_reviews = 0

    async def mesh(self, request):
        response = await super().mesh(request)
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        data = response.json()
        output = json.loads(data["choices"][0]["message"]["content"])
        if "candidate claim Drafter" in system:
            wanted = REVENUE if self.mode == "primary_insufficient" else EMPLOYEES
            output["claims"] = [c for c in output["claims"] if c["claim_text"] == wanted]
        elif "independent support review" in system:
            self.blind_reviews += 1
            if self.mode == "blind_insufficient" and self.blind_reviews == 1:
                output.update(
                    verdict="INSUFFICIENT_EVIDENCE", evidence_ids=[], exact_supporting_quotes=[],
                    explanation="Explicit offline fixture: initial blind review withholds support.",
                )
        data["choices"][0]["message"]["content"] = json.dumps(output)
        return httpx.Response(200, json=data)

    async def tavily(self, request):
        response = await super().tavily(request)
        if request.url.path != "/search":
            return response
        data = response.json()
        original = data["results"][0]
        # Five actual adapter calls per search: the production collector is not batch-based.
        data["results"] = []
        for index in range(5):
            url = original["url"] + f"-source-{index}"
            self.source_texts[url] = self.source_texts[original["url"]]
            data["results"].append({**original, "url": url})
        return httpx.Response(200, json=data)


@pytest.mark.parametrize(
    "mode,expected_model_calls,expected_searches,expected_extracts",
    [("supported", 5, 1, 5), ("primary_insufficient", 7, 2, 10),
     ("blind_insufficient", 8, 2, 10)],
)
async def test_one_claim_plan_exercises_real_graph_within_live_request_envelope(
    settings, mode, expected_model_calls, expected_searches, expected_extracts
):
    config = settings.model_copy(update={
        "enable_live_requests": False,
        "mesh_api_key": None, "mesh_model_id": "offline-p8-readiness",
        "max_llm_calls_per_run": 8, "max_searches_per_run": 2, "max_fetches_per_run": 10,
        "max_claims_per_run": 1, "max_research_subquestions": 1,
        "max_research_tasks_per_run": 2, "max_retries_per_subquestion": 1,
        "max_parallel_fetches": 1, "max_parallel_research_tasks": 1,
        "max_parallel_verifications": 1, "tavily_max_attempts": 1,
    })
    # Supply explicit fake credentials; no local owner configuration is read.
    from pydantic import SecretStr

    config = config.model_copy(update={
        "mesh_api_key": SecretStr("offline-key"), "tavily_api_key": SecretStr("offline-key"),
    })
    providers = OneClaimProviders(mode)
    database = Database(config.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        config, database, mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily), resolver=public_resolver,
    )
    try:
        await runtime.coordinator.start()
        submitted = await runtime.coordinator.submit("OFFLINE one-claim request-envelope witness")
        await runtime.coordinator.wait_idle()
        state = await runtime.coordinator.get(submitted.run_id)
        report = await runtime.coordinator.report(submitted.run_id)
        assert report is not None and report.generation_status == "model_organized"
        assert report.completion_status == "complete" and len(report.verified_findings) == 1
        assert state.budgets.llm_calls == expected_model_calls
        assert state.budgets.search_requests == expected_searches
        assert state.budgets.extract_requests == state.budgets.fetch_units == expected_extracts
        assert state.budgets.llm_calls <= 8
        assert sum(p["max_tokens"] for p in providers.model_payloads) <= 18_432
        assert len(state.retry_history) == (0 if mode == "supported" else 1)
        if state.retry_history:
            assert state.retry_history[0].new_evidence_ids
        claims = await runtime.coordinator.claims(state.run_id)
        receipt = claims.claims[0].attempts[-1].result.support_review_receipt
        assert receipt is not None and receipt.primary_usage_id != receipt.review_usage_id
        events = [e.event for e in state.audit_events]
        assert "support_review_completed" in events and "report_validated" in events
        assert [e.sequence for e in state.audit_events] == list(range(1, len(events) + 1))
        await runtime.reports.validate(report)
    finally:
        await runtime.aclose()
        await database.aclose()
