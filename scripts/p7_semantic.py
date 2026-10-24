"""Replay P6 attacks through the real report path, preserving the failing release gate."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from uuid import UUID

import httpx

from app.agents.synthesizer import SynthesizerAgent
from app.application.budgeted_model import BudgetedModel
from app.application.reports import ReportBuilder
from app.evaluation.dataset import AdversarialDataset, AdversarialScenario, load_dataset
from app.evaluation.harness import create_harness, fixture_settings
from app.infrastructure.database import Database
from app.infrastructure.mesh import MeshLLMAdapter
from app.infrastructure.report_repository import SQLiteReportRepository
from app.workflow import WorkflowRuntime
from scripts.offline_p5 import OfflineP5Providers, public_resolver
from scripts.p6_fixtures import MODEL_ID, OfflineSemanticFixture

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests/evaluation/datasets/adversarial.v1.json"


class ReportAttackProvider(OfflineSemanticFixture):
    def __init__(self, mode, *, malicious_synthesis=False):
        super().__init__(mode)
        self.synthesis_calls = 0
        self.malicious_synthesis = malicious_synthesis

    async def mesh(self, request):
        body = json.loads(request.content)
        if "distinct Synthesizer" not in body["messages"][0]["content"]:
            return await super().mesh(request)
        self.synthesis_calls += 1
        payload = json.loads(body["messages"][1]["content"])
        ids = [c["claim_id"] for c in payload["UNTRUSTED_DATA"]["supported_claims"]]
        output = {
            "executive_summary_claim_ids": ids,
            "sections": [{"heading": "Key findings", "claim_ids": ids}],
        }
        if self.malicious_synthesis:
            output["new_fact"] = "Invented fact that must not enter the report."
        return httpx.Response(
            200,
            json={
                "id": "p7-synthesis-fixture",
                "object": "chat.completion",
                "created": 0,
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(output)},
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        )


class DraftedAttackProvider(OfflineP5Providers):
    """Real planner/drafter/researcher nodes; an injected primary semantic error is reviewed."""

    def __init__(self, case):
        super().__init__()
        self.case = case
        self.semantic = OfflineSemanticFixture("false_support")

    async def mesh(self, request):
        system = json.loads(request.content)["messages"][0]["content"]
        if "distinct Verifier" in system:
            self.calls["verifier"] += 1
            return await self.semantic.mesh(request)
        response = await super().mesh(request)
        if "candidate claim Drafter" in system:
            data = response.json()
            payload = json.loads(json.loads(request.content)["messages"][1]["content"])
            task = payload["tasks"][0]
            output = {
                "claims": [
                    {
                        "claim_text": self.case.claim_text,
                        "originating_research_task_id": task["task_id"],
                        "candidate_evidence_ids": [
                            e["evidence_id"] for e in task["UNTRUSTED_EVIDENCE"]
                        ],
                    }
                ]
            }
            data["choices"][0]["message"]["content"] = json.dumps(output)
            return httpx.Response(200, json=data)
        return response

    async def tavily(self, request):
        response = await super().tavily(request)
        if request.url.path == "/extract":
            data = response.json()
            for row in data["results"]:
                row["raw_content"] = self.case.evidence_text
            return httpx.Response(200, json=data)
        return response


async def trace_drafted_attack(case, directory):
    provider = DraftedAttackProvider(case)
    settings = fixture_settings(
        database_url=f"sqlite:///{(directory / (case.case_id + '.db')).as_posix()}",
        mesh_api_key="offline-key",
        mesh_model_id=MODEL_ID,
        tavily_api_key="offline-key",
        tavily_max_attempts=1,
        max_retries_per_subquestion=0,
    )
    database = Database(settings.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        settings,
        database,
        mesh_transport=httpx.MockTransport(provider.mesh),
        tavily_transport=httpx.MockTransport(provider.tavily),
        resolver=public_resolver,
    )
    try:
        await runtime.coordinator.start()
        submitted = await runtime.coordinator.submit("OFFLINE: assess the claim " + case.claim_text)
        await runtime.coordinator.wait_idle()
        state = await runtime.store.get(submitted.run_id)
        report = await runtime.coordinator.report(submitted.run_id)
        collection = await runtime.claims.collection(submitted.run_id)
        await runtime.reports.validate(report)
        return {
            "case_id": case.case_id,
            "provider_calls": provider.calls,
            "state": state.model_dump(mode="json", exclude={"pending_report_json"}),
            "claims": collection.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
            "drafting_executed": provider.calls["drafter"] == 1,
            "unsupported_reached_verified_findings": bool(report.verified_findings),
        }
    finally:
        await runtime.aclose()
        await database.aclose()


async def trace_to_report(harness, scenario, *, malicious_synthesis=False):
    provider = ReportAttackProvider(scenario.provider_mode, malicious_synthesis=malicious_synthesis)
    observation = await harness.evaluate(
        scenario.case, provider, mutation=scenario.mutation, max_calls=3
    )
    run_id = UUID(observation["prediction"]["run_id"])
    settings = fixture_settings(mesh_api_key="offline-key", mesh_model_id=MODEL_ID)
    mesh = MeshLLMAdapter(settings, mock_transport=httpx.MockTransport(provider.mesh))
    reports = SQLiteReportRepository(harness.database, harness.claims)
    builder = ReportBuilder(
        harness.claims,
        harness.store,
        reports,
        SynthesizerAgent(BudgetedModel(mesh, harness.store, 3)),
    )
    try:
        state = await builder.synthesize(await harness.store.get(run_id))
        state = await builder.validate_report(state)
        report = await reports.get(run_id)
        await reports.validate(report)
    finally:
        await mesh.aclose()
    reached = any(f.claim_text == scenario.case.claim_text for f in report.verified_findings)
    unsafe = scenario.case.expected_verdict.value != "SUPPORTED" and reached
    return {
        "case_id": scenario.case.case_id,
        "challenge": scenario.case.challenge,
        "mode": "OFFLINE_INJECTED_PROVIDER_NOT_MESH_ACCURACY",
        "review_status": scenario.case.review_status,
        "observation": observation,
        "semantic_verifier_rejected": observation["prediction"]["verdict"] != "SUPPORTED",
        "reached_verified_findings": reached,
        "report_validation_detected_semantic_error": False if unsafe else None,
        "structural_report_validation_passed": True,
        "release_blocking_defect_reproduced": unsafe,
        "synthesis_calls": provider.synthesis_calls,
        "synthesis_new_fact_rejected": malicious_synthesis
        and "new_fact" not in report.model_dump(),
        "report": report.model_dump(mode="json"),
        "state": state.model_dump(mode="json", exclude={"pending_report_json"}),
    }


async def run(output: Path):
    dataset = AdversarialDataset.model_validate_json(DATA.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="prooftrail-p7-semantic-") as directory:
        harness = await create_harness(Path(directory) / "trace.db")
        try:
            traces = [await trace_to_report(harness, s) for s in dataset.scenarios]
            supported = load_dataset(ROOT / "tests/evaluation/datasets/claims.v1.json").cases[0]
            synthesis = await trace_to_report(
                harness,
                AdversarialScenario(case=supported, provider_mode="normal"),
                malicious_synthesis=True,
            )
            drafted = [
                await trace_drafted_attack(s.case, Path(directory))
                for s in dataset.scenarios
                if s.case.challenge in {"negation_reversal", "misleading_partial_quote"}
            ]
        finally:
            await harness.database.aclose()
    result = {
        "mode": "offline_synthetic_fault_injection",
        "traces": traces,
        "synthesis_bypass_probe": synthesis,
        "draft_to_final_report_traces": drafted,
        "release_blocked": any(t["release_blocking_defect_reproduced"] for t in traces)
        or any(d["unsupported_reached_verified_findings"] for d in drafted),
        "blocking_cases": [t["case_id"] for t in traces if t["release_blocking_defect_reproduced"]],
        "live_requests": 0,
        "actual_cost_usd": 0,
    }
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-safe", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(2, "Choose a new output path; previous evidence is immutable.\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run(args.output))
    print(
        "Semantic release gate: "
        + (
            "FAIL — unsupported findings reproduced"
            if result["release_blocked"]
            else "PASS for these fixtures"
        )
    )
    return 1 if args.require_safe and result["release_blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
