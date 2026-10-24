"""Owner-only, bounded Mesh evaluation of independently reviewed stored evidence.

Never invoked by offline CI/checks. No Tavily requests, SDK retries or model fallback.
The approval flag records the caller's authorization; it does not certify account pricing.
"""

import argparse
import asyncio
import json
import platform
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from app.application.config import Settings
from app.evaluation.dataset import dataset_digest, load_dataset
from app.evaluation.harness import create_harness
from app.evaluation.review import apply_owner_review
from app.evaluation.scoring import summarize
from app.infrastructure.mesh import MeshLLMAdapter
from scripts.evaluate_p6 import DATA, ROOT, file_digest


def reviewed_selection(
    dataset_path, worksheet, *, approved, max_calls, split="development", allow_held_out=False
):
    if not approved:
        raise ValueError("Explicit owner approval is required; no live request made.")
    if not 1 <= max_calls <= 60:
        raise ValueError("A hard request cap between 1 and 60 is required.")
    if split == "held_out" and not allow_held_out:
        raise ValueError("Held-out evaluation requires explicit --allow-held-out.")
    dataset = load_dataset(dataset_path)
    if worksheet:
        dataset = apply_owner_review(dataset, worksheet)
    eligible = [
        c for c in dataset.cases if c.dataset_split == split and c.review_status == "HUMAN_REVIEWED"
    ]
    if not eligible:
        raise ValueError("No HUMAN_REVIEWED cases in this split; live evaluation refused.")
    selected = eligible[:max_calls // 2]
    if not selected:
        raise ValueError("Two request slots are required for mandatory semantic support review.")
    return dataset, selected


async def run_live(
    dataset_path, worksheet, *, approved, max_calls, split="development", allow_held_out=False
):
    dataset, selected = reviewed_selection(
        dataset_path,
        worksheet,
        approved=approved,
        max_calls=max_calls,
        split=split,
        allow_held_out=allow_held_out,
    )
    settings = Settings(enable_live_requests=True, max_llm_calls_per_run=2)
    if not settings.mesh_api_key or not settings.mesh_model_id:
        raise ValueError("Mesh credentials/model must be configured privately in the environment.")
    started = perf_counter()
    mesh = MeshLLMAdapter(settings)
    with tempfile.TemporaryDirectory(prefix="prooftrail-p6-live-") as directory:
        harness = await create_harness(Path(directory) / "evaluation.db")
        try:
            predictions = [await harness.evaluate(c, mesh_adapter=mesh) for c in selected]
        finally:
            await mesh.aclose()
            await harness.database.aclose()
    counts = sum(r["provider_calls"] for r in predictions)
    if counts > max_calls:
        raise RuntimeError("Live evaluation exceeded its request ceiling.")
    return {
        "mode": "LIVE_MESH_REVIEWED_SNAPSHOTS",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": dataset_digest(dataset),
        "model_identifier": settings.mesh_model_id,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "configuration": {
            "http_timeout_seconds": settings.http_timeout_seconds,
            "llm_requests_per_case": 2,
            "max_calls": max_calls,
            "sdk_retries": 0,
        },
        "prompt_configuration_version": "P4-verifier-unchanged/p6-harness-v1",
        "verifier_source_sha256": file_digest(ROOT / "app/agents/verifier.py"),
        "harness_sha256": file_digest(ROOT / "app/evaluation/harness.py"),
        "split": split,
        "selected_case_ids": [c.case_id for c in selected],
        "selection_method": "Reviewed cases in versioned order, capped before execution",
        "synthetic_case_count": sum(c.evidence_kind == "synthetic" for c in selected),
        "authentic_snapshot_case_count": sum(
            c.evidence_kind == "authentic_snapshot" for c in selected
        ),
        "real_world_accuracy_claim_permitted": False,
        "summary": summarize(predictions),
        "predictions": predictions,
        "duration_seconds": perf_counter() - started,
        "api_usage": {
            "mesh_adapter_invocations": counts,
            "max_calls": max_calls,
            "tavily_requests": 0,
            "recorded_llm_requests": sum(r["prediction"]["llm_requests"] for r in predictions),
            "prompt_tokens": token_total(predictions, "prompt_tokens"),
            "completion_tokens": token_total(predictions, "completion_tokens"),
            "actual_cost_usd": None,
            "cost_note": "Account billing/pricing must be checked by owner; no invented estimate.",
        },
    }


def token_total(predictions, field):
    values = [r["prediction"][field] for r in predictions if r["prediction"]["llm_requests"]]
    return sum(values) if values and all(v is not None for v in values) else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATA / "claims.v1.json")
    parser.add_argument("--review-worksheet", type=Path)
    parser.add_argument("--approved-live", action="store_true")
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--split", choices=("development", "held_out"), default="development")
    parser.add_argument("--allow-held-out", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "results/p6_evaluation_live.json")
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(
            2, "Output already exists; choose a new file before making any live requests.\n"
        )
    try:
        result = asyncio.run(
            run_live(
                args.dataset,
                args.review_worksheet,
                approved=args.approved_live,
                max_calls=args.max_calls,
                split=args.split,
                allow_held_out=args.allow_held_out,
            )
        )
    except ValueError as error:
        parser.exit(2, str(error) + "\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Preserve earlier live sessions; the owner chooses a new filename for another session.
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
