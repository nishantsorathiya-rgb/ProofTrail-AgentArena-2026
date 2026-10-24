"""Paired actual workflow under explicit synthetic HTTP fixtures; immutable raw run directories."""

import argparse
import asyncio
import json
import platform
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter

import httpx
from sqlalchemy import select

from app.agents.drafter import ClaimDrafter
from app.application.budgeted_model import BudgetedModel
from app.evaluation.benchmark import BenchmarkDataset, evaluate_assertions, judge
from app.evaluation.harness import fixture_settings
from app.infrastructure.database import Database
from app.infrastructure.evidence_models import UsageRow
from app.infrastructure.workflow_models import ModelUsageRow
from app.workflow import WorkflowRuntime
from scripts.offline_p5 import OfflineP5Providers, public_resolver

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests/evaluation/datasets/benchmark.v1.json"


class PairedFixture(OfflineP5Providers):
    def __init__(self, case):
        super().__init__()
        self.case = case
        self.initial_empty, self.retry_result = case.initial_empty, case.retry_result
        self.initial_versions = []

    async def mesh(self, request):
        response = await super().mesh(request)
        body = json.loads(request.content)
        if (
            self.case.fault == "negation"
            and "candidate claim Drafter" in body["messages"][0]["content"]
        ):
            data = response.json()
            output = json.loads(data["choices"][0]["message"]["content"])
            prototype = output["claims"][0]
            output["claims"].append(
                {**prototype, "claim_text": "AlphaCo did not employ 42 people in 2024."}
            )
            data["choices"][0]["message"]["content"] = json.dumps(output)
            return httpx.Response(200, json=data)
        return response

    async def tavily(self, request):
        response = await super().tavily(request)
        if request.url.path == "/extract":
            data = response.json()
            for row in data["results"]:
                if self.case.fault == "conditional" and row["url"].endswith("/initial"):
                    row["raw_content"] = "Conditional only; if approved: " + row["raw_content"]
                if row["url"].endswith("/initial"):
                    self.initial_versions.append(
                        {
                            "original_url": row["url"],
                            "content_sha256": sha256(row["raw_content"].encode()).hexdigest(),
                            "exact_content": row["raw_content"],
                        }
                    )
            return httpx.Response(200, json=data)
        return response


async def run_arm(case, dataset, arm, directory):
    providers = PairedFixture(case)
    settings = fixture_settings(
        database_url=f"sqlite:///{(directory / (arm + '.db')).as_posix()}",
        mesh_api_key="offline-key",
        mesh_model_id="offline-mesh-p5-fixture",
        tavily_api_key="offline-key",
        tavily_max_attempts=1,
    )
    database = Database(settings.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        settings,
        database,
        mesh_transport=httpx.MockTransport(providers.mesh),
        tavily_transport=httpx.MockTransport(providers.tavily),
        resolver=public_resolver,
        phase="P3" if arm == "baseline" else "P5",
    )
    try:
        await runtime.coordinator.start()
        started = perf_counter()
        submitted = await runtime.coordinator.submit(case.question)
        await runtime.coordinator.wait_idle()
        state = await runtime.store.get(submitted.run_id)
        if arm == "baseline":
            await runtime.store.save(state, agent="drafter", event="baseline_drafting_started")
            drafter = ClaimDrafter(
                BudgetedModel(runtime.mesh, runtime.store, 12),
                runtime.claims,
                max_claims=18,
                max_chars=24000,
            )
            candidates = await drafter.draft(state.run_id, case.question, state.subquestions)
            assertions = []
            for candidate in candidates:
                evidence = await runtime.claims.evidence_for(candidate)
                assertions.append(
                    {
                        "claim_text": candidate.claim_text,
                        "claim_id": str(candidate.claim_id),
                        "cited_texts": [e.text for e in evidence],
                        "evidence_ids": [str(e.evidence_id) for e in evidence],
                        "original_urls": [e.original_url for e in evidence],
                        "structurally_valid": bool(evidence),
                        "verification": "NOT_PERFORMED",
                    }
                )
            state = state.evolve(**await runtime.store.metrics(state.run_id, state.budgets))
            state = await runtime.store.save(
                state,
                agent="drafter",
                event="baseline_drafting_completed",
                details={"assertions": len(assertions)},
            )
            report = None
        else:
            report = await runtime.coordinator.report(state.run_id)
            await runtime.reports.validate(report)
            assertions = []
            for finding in report.verified_findings:
                citations = [
                    c for c in report.supporting_citations if c.claim_id == finding.claim_id
                ]
                evidence = await runtime.store.evidence(state.run_id)
                by_id = {p.passage_id: p.text for p in evidence.passages}
                assertions.append(
                    {
                        "claim_text": finding.claim_text,
                        "claim_id": str(finding.claim_id),
                        "cited_texts": [by_id[c.evidence_id] for c in citations],
                        "evidence_ids": [str(c.evidence_id) for c in citations],
                        "original_urls": [c.original_url for c in citations],
                        "structurally_valid": bool(citations),
                        "verification": "MODEL_SUPPORTED",
                    }
                )
        elapsed = perf_counter() - started
        claims = await runtime.coordinator.claims(state.run_id)
        evidence = await runtime.store.evidence(state.run_id)
        text_by_id = {p.passage_id: p.text for p in evidence.passages}
        async with database.sessions() as session:
            model_usage = (
                await session.scalars(
                    select(ModelUsageRow).where(ModelUsageRow.run_id == str(state.run_id))
                )
            ).all()
            retrieval_usage = (
                await session.scalars(select(UsageRow).where(UsageRow.run_id == str(state.run_id)))
            ).all()
        retries = []
        for retry in state.retry_history:
            changes = []
            for record in claims.claims:
                for attempt in record.attempts:
                    result = attempt.result
                    if result and result.research_retry_id == retry.retry_id:
                        new_cited = set(result.evidence_ids) & set(retry.new_evidence_ids)
                        independent = judge(
                            record.candidate.claim_text,
                            [text_by_id[p] for p in result.evidence_ids],
                            dataset.rules,
                        )
                        changed = (
                            result.previous_verdict == "INSUFFICIENT_EVIDENCE"
                            and result.verdict.value == "SUPPORTED"
                            and bool(new_cited)
                            and independent == "SUPPORTED"
                        )
                        changes.append(
                            {
                                "claim_text": record.candidate.claim_text,
                                "initial_verdict": result.previous_verdict,
                                "reverification_verdict": result.verdict.value,
                                "independent_proposed_label": independent,
                                "novel_cited_evidence_ids": [str(p) for p in new_cited],
                                "provisional_recovery": changed,
                                "human_confirmed_recovery": None,
                            }
                        )
            retries.append({"record": retry.model_dump(mode="json"), "changes": changes})
        return {
            "arm": arm,
            "question": case.question,
            "model_configuration": {
                "requested_model": settings.mesh_model_id,
                "temperature": 0,
                "fallback": False,
                "sdk_retries": 0,
            },
            "initial_evidence": providers.initial_versions,
            "quality": evaluate_assertions(assertions, case, dataset),
            "end_to_end_seconds": elapsed,
            "provider_calls": providers.calls,
            "state": state.model_dump(mode="json", exclude={"pending_report_json"}),
            "claims": claims.model_dump(mode="json"),
            "report": report.model_dump(mode="json") if report else None,
            "retry_effectiveness": retries,
            "model_request_timings": [
                {
                    "usage_id": m.usage_id,
                    "status": m.status,
                    "elapsed_seconds": m.elapsed_seconds,
                    "error_code": m.error_code,
                    "model": m.model,
                }
                for m in model_usage
            ],
            "retrieval_request_timings": [
                {
                    "operation": u.operation,
                    "status": u.status,
                    "elapsed_seconds": u.elapsed_seconds,
                    "error_code": u.error_code,
                    "fixture_credits": u.reported_credits,
                }
                for u in retrieval_usage
            ],
            "api_usage": {
                "model_requests": state.budgets.llm_calls,
                "search_requests": state.budgets.search_requests,
                "extract_requests": state.budgets.extract_requests,
                "cache_hits": state.cache_hits,
                "fixture_prompt_tokens": state.prompt_tokens,
                "fixture_completion_tokens": state.completion_tokens,
                "actual_cost_usd": 0,
                "estimated_live_cost_usd": None,
            },
        }
    finally:
        await runtime.aclose()
        await database.aclose()


async def benchmark(run_directory):
    run_directory.mkdir(parents=True, exist_ok=False)
    dataset = BenchmarkDataset.model_validate_json(DATA.read_text(encoding="utf-8"))
    started = perf_counter()
    pairs = []
    with tempfile.TemporaryDirectory(prefix="prooftrail-p7-paired-") as temp:
        for index, case in enumerate(dataset.cases):
            directory = Path(temp) / case.case_id
            directory.mkdir()
            # Alternate order rather than always charging one arm with first-use initialization.
            arms = ("baseline", "prooftrail") if index % 2 == 0 else ("prooftrail", "baseline")
            results = {arm: await run_arm(case, dataset, arm, directory) for arm in arms}
            left, right = results["baseline"], results["prooftrail"]
            same = left["initial_evidence"] == right["initial_evidence"]
            if not same or left["model_configuration"] != right["model_configuration"]:
                raise ValueError("Unfair pair: initial evidence or model differs.")
            pair = {
                "case_id": case.case_id,
                "execution_order": list(arms),
                "identical_initial_evidence": same,
                **results,
                "incremental_usage": {
                    key: right["api_usage"][key] - left["api_usage"][key]
                    for key in ("model_requests", "search_requests", "extract_requests")
                },
                "latency_delta_seconds": right["end_to_end_seconds"] - left["end_to_end_seconds"],
            }
            with (run_directory / (case.case_id + ".json")).open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(pair, indent=2) + "\n")
            pairs.append(pair)
    result = {
        "mode": "OFFLINE_SYNTHETIC_PAIRED_BENCHMARK_NOT_LIVE_COMPETITIVE_ACCURACY",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": sha256(DATA.read_bytes()).hexdigest(),
        "review_status": dataset.review_status,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "prompt_sha256": {
            name: sha256((ROOT / f"app/agents/{name}.py").read_bytes()).hexdigest()
            for name in ("planner", "drafter", "verifier", "synthesizer", "followup_planner")
        },
        "fixture_sha256": sha256((ROOT / "scripts/offline_p5.py").read_bytes()).hexdigest(),
        "pairs": pairs,
        "aggregate_provisional_metrics": {
            arm: aggregate(pairs, arm) for arm in ("baseline", "prooftrail")
        },
        "duration_seconds": perf_counter() - started,
        "human_reviewed_results": None,
        "live_model_results": None,
        "actual_api_cost_usd": 0,
        "live_requests": 0,
        "estimated_live_cost_formula": "input_tokens/1e6 * approved_input_rate + "
        "output_tokens/1e6 * approved_output_rate + search_credits * approved_credit_rate; "
        "rates unknown, and fixture token counts are not live estimates.",
        "limitations": [
            "All rubrics and evidence are synthetic and unreviewed",
            "Five variants concern one fictional company; not representative competition accuracy",
            "Baseline asserts drafter outputs without verification, synthesis or research retry",
            "ProofTrail has additional verification, follow-up and synthesis requests",
            "Known semantic false approvals remain visible in prooftrail findings",
        ],
    }
    with (run_directory / "comparison.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    return result


def aggregate(pairs, arm):
    observations = [p[arm]["quality"]["provisional_synthetic_metrics"] for p in pairs]
    emitted = sum(r["assertions"] for r in observations)
    supported = sum(r["supported"] for r in observations)
    unsupported = sum(r["unsupported"] for r in observations)
    topics = sum(len(r["expected_subtopics"]) for r in observations)
    return {
        "assertions": emitted,
        "supported": supported,
        "unsupported": unsupported,
        "supported_final_claim_rate": supported / emitted if emitted else None,
        "unsupported_final_claim_rate": unsupported / emitted if emitted else None,
        "research_completeness": sum(len(r["covered_subtopics"]) for r in observations) / topics,
        "expected_subtopic_instances": topics,
        "total_latency_seconds": sum(p[arm]["end_to_end_seconds"] for p in pairs),
        "total_model_requests": sum(p[arm]["api_usage"]["model_requests"] for p in pairs),
        "total_search_requests": sum(p[arm]["api_usage"]["search_requests"] for p in pairs),
        "total_extract_requests": sum(p[arm]["api_usage"]["extract_requests"] for p in pairs),
        "actual_api_cost_usd": 0,
        "estimated_live_cost_usd": None,
        "interpretation": "Pooled synthetic fixture counts; not reviewed model semantic accuracy",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(benchmark(args.run_directory))
    print(
        f"Paired benchmark: {len(result['pairs'])} immutable offline pairs; live calls 0; cost $0."
    )


if __name__ == "__main__":
    main()
