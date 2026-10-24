"""Reproducible OFFLINE evaluation; no live networking or paid-call mode exists here."""

import argparse
import asyncio
import json
import platform
import tempfile
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from statistics import median
from time import perf_counter

from app.agents.verifier import SUPPORT_REVIEW_PROMPT, SYSTEM_PROMPT
from app.evaluation.citation_review import ensure_replaceable, export_worksheet
from app.evaluation.dataset import AdversarialDataset, dataset_digest, load_dataset
from app.evaluation.harness import create_harness
from app.evaluation.review import apply_owner_review
from app.evaluation.scoring import summarize
from scripts.p6_fixtures import MODEL_ID, POLICY_VERSION, OfflineSemanticFixture
from scripts.p6_workflows import WorkflowDataset, evaluate_workflow

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests/evaluation/datasets"


def file_digest(path):
    return sha256(path.read_bytes()).hexdigest()


def markdown_report(result):
    core = result["verification"]
    provisional = core["provisional_fixture_agreement_NOT_model_accuracy"]
    reviewed = core["reviewed_ground_truth_metrics"]
    reviewed_accuracy = reviewed["classification_accuracy"] if reviewed["case_count"] else "N/A"
    rows = [
        "# P6 offline fixture evaluation",
        "",
        "These are synthetic fixtures and mocked model responses. They do not measure Mesh "
        "semantic accuracy or real-world research quality.",
        "",
        f"Dataset: `{result['metadata']['dataset_version']}`; "
        f"split: {result['metadata']['split']}.",
        f"Human-reviewed scored cases: **{reviewed['case_count']}**. "
        f"Reviewed accuracy: **{reviewed_accuracy}**.",
        "Live requests: **0**. Actual API cost: **$0**.",
        "",
        "## Provisional controlled-fixture agreement (unreviewed labels)",
        "",
        f"Cases: {provisional['case_count']}; accuracy/agreement: "
        f"{provisional['classification_accuracy']}; macro F1: {provisional['macro_f1']}.",
        f"False SUPPORTED: {provisional['false_supported_verdicts']} / "
        f"{provisional['false_supported_rate_denominator']} non-supported cases.",
        "",
        "| Proposed class | Precision | Recall | F1 | Cases |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, scores in provisional["per_class"].items():
        rows.append(
            f"| {label} | {scores['precision']} | {scores['recall']} | "
            f"{scores['f1']} | {scores['actual_count']} |"
        )
    matrix = provisional["confusion_matrix"]
    columns = list(next(iter(matrix.values())))
    rows += [
        "",
        "## Confusion matrix",
        "",
        "Expected \\ predicted; errors are separate columns.",
        "",
        "| Expected | " + " | ".join(columns) + " |",
        "|---|" + "---:|" * len(columns),
    ]
    rows += [
        "| " + label + " | " + " | ".join(str(row[c]) for c in columns) + " |"
        for label, row in matrix.items()
    ]
    adversarial = result["adversarial"]
    rows += [
        "",
        "## Separate adversarial/fault-injection suite",
        "",
        f"Scenarios: {len(result['adversarial_predictions'])}. Invalid proposed citations: "
        f"{adversarial['structural_citations']['invalid_citation_count']}.",
        f"Operational errors: {len(adversarial['operational_failures'])}; validation rejections: "
        f"{len(adversarial['validation_rejections'])}.",
        "Two deliberately malicious semantic responses use real quotations to falsely approve "
        "negated/conditional claims. Citation gates accept those structurally valid responses. "
        "This measures the limits of deterministic gates; it is not a measured Mesh failure rate.",
        "",
        "| Failure case | Outcome | Verdict | Reason |",
        "|---|---|---|---|",
    ]
    for row in adversarial["failure_examples"]:
        p = row["prediction"]
        rows.append(
            f"| {row['case_id']} | {p['outcome']} | {p['verdict']} | "
            f"{p['rejection_reason'] or 'wrong injected semantic decision'} |"
        )
    rows += [
        "",
        "## Actual workflow fixture measurements",
        "",
        "Expected subtopics are UNREVIEWED. Coverage below is a provisional exact-fact rubric, "
        "not a citation-count proxy or reviewed real-world completeness score.",
        "",
        "| Case | Fixture coverage | Total seconds | LLM | Search | Extract | Audit |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in result["workflows"]:
        state, usage = row["state"], row["state"]["budgets"]
        rows.append(
            f"| {row['case_id']} | {row['provisional_fixture_coverage']} | "
            f"{state['total_duration_seconds']:.6f} | {usage['llm_calls']} | "
            f"{usage['search_requests']} | {usage['extract_requests']} | "
            f"{row['audit_integrity']} |"
        )
    rows += [
        "",
        f"Evaluation wall duration: {result['duration_seconds']:.6f} seconds. "
        "Local fixture timings include SQLite/SDK/graph work; no live speedup is claimed.",
        "",
        "JSON preserves all predictions, failure examples, exact quotes, hashes, offsets, "
        "attempt IDs, state/audit traces, retries and usage. See evaluation_methodology.md "
        "for denominators, review gates and limitations.",
        "",
    ]
    return "\n".join(rows)


async def evaluate(
    dataset_path=DATA / "claims.v1.json",
    *,
    split="development",
    allow_held_out=False,
    review_path=None,
    include_workflows=True,
):
    if split in {"all", "held_out"} and not allow_held_out:
        raise ValueError("Held-out execution requires --allow-held-out; never use it for tuning.")
    dataset = load_dataset(dataset_path)
    dataset.validate_target()
    if review_path:
        dataset = apply_owner_review(dataset, review_path)
    selected = [
        c
        for c in dataset.cases
        if (split == "all" or c.dataset_split == split)
        and c.review_status not in {"AMBIGUOUS", "EXCLUDED"}
    ]
    adversarial_data = AdversarialDataset.model_validate_json(
        (DATA / "adversarial.v1.json").read_text(encoding="utf-8")
    )
    workflow_data = WorkflowDataset.model_validate_json(
        (DATA / "workflows.v1.json").read_text(encoding="utf-8")
    )
    # Freeze hashes before executing either split; no label-derived prompts/provider tuning.
    metadata = {
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": dataset_digest(dataset),
        "dataset_file_sha256": file_digest(dataset_path),
        "owner_worksheet_sha256": file_digest(review_path) if review_path else None,
        "adversarial_dataset_sha256": file_digest(DATA / "adversarial.v1.json"),
        "workflow_dataset_sha256": file_digest(DATA / "workflows.v1.json"),
        "model_identifier": MODEL_ID,
        "prompt_version": "P4-verifier-unchanged",
        "verifier_prompt_sha256": sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "support_review_prompt_sha256": sha256(SUPPORT_REVIEW_PROMPT.encode()).hexdigest(),
        "support_policy_version": "blind_semantic_support_review_v1",
        "workflow_prompt_sha256": {
            name: file_digest(ROOT / f"app/agents/{name}.py")
            for name in ("planner", "drafter", "verifier", "followup_planner", "synthesizer")
        },
        "fixture_policy_version": POLICY_VERSION,
        "fixture_policy_sha256": file_digest(ROOT / "scripts/p6_fixtures.py"),
        "workflow_fixture_policy_sha256": file_digest(ROOT / "scripts/offline_p5.py"),
        "configuration_version": "p6-offline-harness-v1",
        "evaluation_code_sha256": {
            str(path.relative_to(ROOT)): file_digest(path)
            for path in sorted((ROOT / "app/evaluation").glob("*.py"))
        },
        "configuration": {
            "verification_llm_budget_per_run": 2,
            "workflow_llm_budget": 12,
            "workflow_retries_per_area": 2,
            "verifier_concurrency": 3,
            "live_requests_enabled": False,
        },
        "split": split,
        "held_out_used_for_tuning": False,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": {p: version(p) for p in ("pydantic", "SQLAlchemy", "langgraph", "openai")},
        "review_status_counts": dict(Counter(c.review_status for c in dataset.cases)),
        "mode": "OFFLINE_SYNTHETIC_FIXTURES_NOT_MESH_ACCURACY",
        "live_evaluation_ready": False,
        "live_evaluation_blockers": [
            "Owner-reviewed independent evidence and labels required",
            "Explicit paid-call approval and capped live session required",
        ],
        "git_revision": None,
    }
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix="prooftrail-p6-") as directory:
        harness = await create_harness(Path(directory) / "verification.db")
        try:
            predictions = [await harness.evaluate(c, OfflineSemanticFixture()) for c in selected]
            adversarial_predictions = []
            for scenario in adversarial_data.scenarios:
                row = await harness.evaluate(
                    scenario.case,
                    OfflineSemanticFixture(scenario.provider_mode),
                    mutation=scenario.mutation,
                )
                row["challenge"] = scenario.case.challenge
                row["injected_provider_mode"] = scenario.provider_mode
                adversarial_predictions.append(row)
        finally:
            await harness.database.aclose()
        workflows = (
            [
                await evaluate_workflow(c, workflow_data, Path(directory))
                for c in workflow_data.cases
            ]
            if include_workflows
            else []
        )
    all_predictions = predictions + adversarial_predictions
    durations = [r["prediction"]["verification_duration_ms"] for r in all_predictions]
    return {
        "metadata": metadata,
        "verification": summarize(predictions),
        "verification_by_split": {
            s: summarize([r for r in predictions if r["dataset_split"] == s])
            for s in ("development", "held_out")
        },
        "predictions": predictions,
        "adversarial": summarize(adversarial_predictions),
        "adversarial_predictions": adversarial_predictions,
        "workflows": workflows,
        "duration_seconds": perf_counter() - started,
        "performance": {
            "claims_attempted": len(all_predictions),
            "verification_duration_ms_sum": sum(durations),
            "verification_duration_ms_median": median(durations) if durations else None,
            "persistence_duration_ms_sum": sum(
                r["prediction"]["persistence_duration_ms"] for r in all_predictions
            ),
            "estimated_input_tokens_char_heuristic": sum(
                r["prediction"]["estimated_input_tokens"] or 0 for r in all_predictions
            ),
            "note": "Local offline measurements only; no performance improvement claimed.",
        },
        "api_usage": {
            "live_mesh_calls": 0,
            "live_tavily_calls": 0,
            "actual_api_cost_usd": 0,
            "mock_verifier_calls": sum(r["provider_calls"] for r in all_predictions),
            "mock_workflow_calls": {
                key: sum(r["provider_calls"][key] for r in workflows)
                for key in (
                    "planner",
                    "drafter",
                    "verifier",
                    "followup",
                    "synthesizer",
                    "search",
                    "extract",
                )
            },
            "fixture_prompt_tokens": sum(
                r["prediction"]["prompt_tokens"] or 0 for r in all_predictions
            )
            + sum(r["state"]["prompt_tokens"] or 0 for r in workflows),
            "fixture_completion_tokens": sum(
                r["prediction"]["completion_tokens"] or 0 for r in all_predictions
            )
            + sum(r["state"]["completion_tokens"] or 0 for r in workflows),
            "token_note": "Fixed mock metadata; not actual Mesh token consumption or pricing.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATA / "claims.v1.json")
    parser.add_argument(
        "--split", choices=("development", "held_out", "all"), default="development"
    )
    parser.add_argument("--allow-held-out", action="store_true")
    parser.add_argument("--review-worksheet", type=Path)
    parser.add_argument("--no-workflows", action="store_true")
    parser.add_argument(
        "--citation-worksheet", type=Path, default=ROOT / "results/p6_citation_owner_review.csv"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "results/p6_evaluation_offline.json")
    parser.add_argument("--markdown", type=Path, default=ROOT / "results/p6_metrics.md")
    args = parser.parse_args()
    try:
        ensure_replaceable(args.citation_worksheet)
        result = asyncio.run(
            evaluate(
                args.dataset,
                split=args.split,
                allow_held_out=args.allow_held_out,
                review_path=args.review_worksheet,
                include_workflows=not args.no_workflows,
            )
        )
    except ValueError as error:
        parser.exit(2, f"Evaluation configuration rejected: {error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(markdown_report(result), encoding="utf-8")
    args.citation_worksheet.parent.mkdir(parents=True, exist_ok=True)
    export_worksheet(
        result["predictions"] + result["adversarial_predictions"], args.citation_worksheet
    )
    print(
        f"P6 offline evaluation: {len(result['predictions'])} core cases, "
        f"{len(result['adversarial_predictions'])} adversarial scenarios, "
        f"{len(result['workflows'])} workflows; reviewed accuracy N/A until owner review."
    )
    print(f"Saved {args.output} and {args.markdown}; live calls 0; cost $0.")


if __name__ == "__main__":
    main()
