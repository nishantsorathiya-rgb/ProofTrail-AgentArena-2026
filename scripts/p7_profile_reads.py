"""Measure repeated evidence integrity reads before/after a bounded SQL read optimization."""

import argparse
import asyncio
import json
import platform
import tempfile
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import UUID

from sqlalchemy import event

from app.evaluation.dataset import EvaluationCase
from app.evaluation.harness import create_harness
from scripts.p6_fixtures import OfflineSemanticFixture


async def measure(output, label):
    content = "In 2024, Test Archive recorded 42 records. " + "Synthetic context padding. " * 410
    case = EvaluationCase(
        case_id="P7-PROFILE",
        claim_text="In 2024, Test Archive recorded 42 records.",
        evidence_text=content,
        evidence_source="fixture:p7/profile-six-passages",
        evidence_provenance="Explicit synthetic context; no retrieval or live inference.",
        expected_verdict="SUPPORTED",
        explanation="Controlled profiling fixture only.",
        review_status="UNREVIEWED",
        dataset_split="development",
        evidence_kind="synthetic",
        family_id="profile",
    )
    with tempfile.TemporaryDirectory(prefix="prooftrail-p7-profile-") as directory:
        harness = await create_harness(Path(directory) / "profile.db")
        try:
            observed = await harness.evaluate(case, OfflineSemanticFixture())
            collection = await harness.claims.collection(UUID(observed["prediction"]["run_id"]))
            candidate = collection.claims[0].candidate
            counter = {"queries": 0}

            def count(*_):
                counter["queries"] += 1

            event.listen(harness.database.engine.sync_engine, "before_cursor_execute", count)
            runs, payloads = [], []
            for _ in range(9):
                counter["queries"] = 0
                started = perf_counter()
                evidence = await harness.claims.evidence_for(candidate)
                runs.append({"seconds": perf_counter() - started, "queries": counter["queries"]})
                payloads.append([e.model_dump(mode="json") for e in evidence])
            event.remove(harness.database.engine.sync_engine, "before_cursor_execute", count)
            assert all(p == payloads[0] for p in payloads)
            result = {
                "label": label,
                "mode": "offline SQLite microbenchmark",
                "python": platform.python_version(),
                "source_chars": len(content),
                "passages": len(evidence),
                "runs": runs,
                "median_seconds_excluding_warmup": median(r["seconds"] for r in runs[1:]),
                "median_queries": median(r["queries"] for r in runs[1:]),
                "evidence_content": [e.text for e in evidence],
                "all_reads_identical": True,
                "actual_api_cost_usd": 0,
            }
        finally:
            await harness.database.aclose()
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("label", "passages", "median_queries", "median_seconds_excluding_warmup")
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(measure(args.output, args.label))


if __name__ == "__main__":
    main()
