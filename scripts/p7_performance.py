"""Local profiles with injected provider delay and fixture token metadata labeled explicitly."""

import argparse
import asyncio
import json
import tempfile
from hashlib import sha256
from pathlib import Path
from time import perf_counter

import httpx

from app.evaluation.benchmark import BenchmarkCase, BenchmarkDataset
from app.evaluation.harness import fixture_settings
from app.infrastructure.database import Database
from app.workflow import WorkflowRuntime
from scripts.offline_p5 import OfflineP5Providers, public_resolver
from scripts.p7_benchmark import DATA, run_arm


class DelayedProviders(OfflineP5Providers):
    def __init__(self):
        super().__init__()
        self.active = self.peak = 0

    async def mesh(self, request):
        data = (await super().mesh(request)).json()
        output = {
            "goal": "Explicit synthetic independent I/O profile",
            "subquestions": [
                {
                    "question": f"Retrieve independent synthetic topic {i}",
                    "search_query": f"independent synthetic query {i}",
                    "evidence_requirement": "Exact original fixture text",
                }
                for i in range(3)
            ],
        }
        data["choices"][0]["message"]["content"] = json.dumps(output)
        return httpx.Response(200, json=data)

    async def tavily(self, request):
        self.active += 1
        self.peak = max(self.active, self.peak)
        try:
            await asyncio.sleep(0.03)  # Deliberate simulated I/O, never advertised as live latency.
            body = json.loads(request.content)
            if request.url.path == "/search":
                self.calls["search"] += 1
                url = (
                    "https://example.com/performance/" + sha256(body["query"].encode()).hexdigest()
                )
                self.source_texts[url] = "EXPLICIT SYNTHETIC PROFILE SOURCE: " + body["query"]
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"url": url, "title": "SYNTHETIC", "content": "discovery clue only"}
                        ]
                    },
                )
            self.calls["extract"] += 1
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": u, "raw_content": self.source_texts[u]} for u in body["urls"]
                    ],
                    "failed_results": [],
                },
            )
        finally:
            self.active -= 1


async def concurrency_profile(directory, limit):
    provider = DelayedProviders()
    settings = fixture_settings(
        database_url=f"sqlite:///{(directory / f'parallel-{limit}.db').as_posix()}",
        mesh_api_key="offline-key",
        mesh_model_id="offline-mesh-p5-fixture",
        tavily_api_key="offline-key",
        max_parallel_research_tasks=limit,
        max_parallel_fetches=3,
        tavily_max_attempts=1,
    )
    database = Database(settings.database_url)
    await database.initialize()
    runtime = WorkflowRuntime(
        settings,
        database,
        mesh_transport=httpx.MockTransport(provider.mesh),
        tavily_transport=httpx.MockTransport(provider.tavily),
        resolver=public_resolver,
        phase="P3",
    )
    try:
        await runtime.coordinator.start()
        started = perf_counter()
        submitted = await runtime.coordinator.submit("OFFLINE: profile three independent sources")
        await runtime.coordinator.wait_idle()
        state = await runtime.store.get(submitted.run_id)
        return {
            "configured_research_concurrency": limit,
            "observed_peak_http_requests": provider.peak,
            "simulated_delay_seconds_per_request": 0.03,
            "wall_seconds": perf_counter() - started,
            "state": state.model_dump(mode="json", exclude={"pending_report_json"}),
            "provider_calls": provider.calls,
        }
    finally:
        await runtime.aclose()
        await database.aclose()


async def run(output, before_path, after_path, comparison_path):
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    assert before["evidence_content"] == after["evidence_content"]
    dataset = BenchmarkDataset.model_validate_json(DATA.read_text(encoding="utf-8"))
    cache_case = BenchmarkCase(
        case_id="P7-CACHE",
        question="AlphaCo 2024 employees and revenue",
        retry_result="duplicate_url",
        expected_subtopics=dataset.cases[0].expected_subtopics,
    )
    with tempfile.TemporaryDirectory(prefix="prooftrail-p7-performance-") as directory:
        cache = await run_arm(cache_case, dataset, "prooftrail", Path(directory))
        concurrency = [await concurrency_profile(Path(directory), c) for c in (1, 3)]
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    stages = [
        {
            "case_id": p["case_id"],
            "stages": {
                field: p["prooftrail"]["state"][field]
                for field in (
                    "planning_duration_seconds",
                    "research_duration_seconds",
                    "drafting_duration_seconds",
                    "verification_duration_seconds",
                    "retry_duration_seconds",
                    "synthesis_duration_seconds",
                    "report_validation_duration_seconds",
                )
            },
            "latency_delta_seconds": p["latency_delta_seconds"],
        }
        for p in comparison["pairs"]
    ]
    result = {
        "mode": "OFFLINE_LOCAL_MEASUREMENTS_NOT_LIVE_SPEEDUP",
        "read_profile_before": before,
        "read_profile_after": after,
        "queries_saved_per_six_passage_read": before["median_queries"] - after["median_queries"],
        "median_read_time_change_seconds": after["median_seconds_excluding_warmup"]
        - before["median_seconds_excluding_warmup"],
        "optimization": "Session-local bulk passage/source/extraction/version-link reads; all "
        "hash, run/task ownership, stable-ID, exact passage and version checks retained",
        "retrieval_cache_profile": cache,
        "existing_concurrency_profiles": concurrency,
        "graph_stage_profiles": stages,
        "actual_api_cost_usd": 0,
        "limitations": [
            "Read microbenchmark has nine samples per version, first excluded as warmup",
            "Sequential before/after on one machine is not a statistical live speedup claim",
            "Concurrency comparison uses explicitly injected 30ms network delay",
            "Concurrency and cache were already implemented; no claim they are new optimizations",
            "Verification and integrity checks were not disabled or weakened",
        ],
    }
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(
        "Performance recorded: queries "
        + str(before["median_queries"])
        + " -> "
        + str(after["median_queries"])
        + "; live calls 0."
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--comparison", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output, args.before, args.after, args.comparison))


if __name__ == "__main__":
    main()
