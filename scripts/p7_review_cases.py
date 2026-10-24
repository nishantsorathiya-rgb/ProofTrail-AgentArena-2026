"""Local owner review with explicit accept/correct/ambiguous/exclude controls; no auto-review."""

import argparse
import csv
import io
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.evaluation.dataset import EvaluationCase, load_dataset
from app.evaluation.review import apply_owner_review

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "tests/evaluation/datasets/claims.v1.json"


def review_rows(rows, *, split, reviewer, ask=input, show=print):
    if not reviewer.strip():
        raise ValueError("A reviewer identity is required; no automatic identity is supplied.")
    decisions = []
    for row in rows:
        if row["dataset_split"] != split or row["review_status"] != "UNREVIEWED":
            continue
        show(f"\n{row['case_id']} | {row['dataset_split']} | {row['evidence_kind']}")
        for field in (
            "claim_text",
            "evidence_text",
            "evidence_source",
            "evidence_provenance",
            "expected_verdict",
            "explanation",
            "review_status",
            "reviewer_notes",
        ):
            show(f"{field}: {row[field]}")
        answer = (
            ask(
                "[y] accept proposed; [s/c/i] correct; [a] ambiguous; [x] exclude; "
                "[enter] skip; [q] save and quit: "
            )
            .strip()
            .casefold()
        )
        if answer == "q":
            break
        if answer not in {"y", "s", "c", "i", "a", "x"}:
            continue
        notes = ask("Reason/reviewer notes (required for a correction or exclusion): ").strip()
        if answer in {"s", "c", "i", "x"} and not notes:
            show("No change saved: a correction/exclusion requires a reason.")
            continue
        if answer in {"y", "s", "c", "i"}:
            row["review_status"] = "HUMAN_REVIEWED"
            if answer != "y":
                row["expected_verdict"] = {
                    "s": "SUPPORTED",
                    "c": "CONTRADICTED",
                    "i": "INSUFFICIENT_EVIDENCE",
                }[answer]
                row["explanation"] = notes
        else:
            row["review_status"] = "AMBIGUOUS" if answer == "a" else "EXCLUDED"
            if answer == "x":
                row["exclusion_reason"] = notes
        row["reviewer"], row["reviewer_notes"] = reviewer, notes
        EvaluationCase.model_validate(row)
        decisions.append(row["case_id"])
    return decisions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--worksheet", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--split", choices=("development", "held_out"), default="development")
    parser.add_argument("--allow-held-out", action="store_true")
    args = parser.parse_args()
    if args.split == "held_out" and not args.allow_held_out:
        parser.exit(
            2, "Held-out review must be explicit and separate from development diagnosis.\n"
        )
    dataset = load_dataset(args.dataset)
    if args.worksheet.exists():
        apply_owner_review(dataset, args.worksheet)
        with args.worksheet.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    else:
        rows = [c.model_dump(mode="json") for c in dataset.cases]
    changed = review_rows(rows, split=args.split, reviewer=args.reviewer)
    if not changed:
        print("No approvals/labels changed; no worksheet written.")
        return
    if args.worksheet.exists():
        backup = args.worksheet.with_suffix(
            "." + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".csv"
        )
        shutil.copy2(args.worksheet, backup)
    args.worksheet.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(EvaluationCase.model_fields))
    writer.writeheader()
    writer.writerows(rows)
    args.worksheet.write_text(buffer.getvalue(), encoding="utf-8")
    print(f"Saved {len(changed)} explicit reviewer decisions. Original 60-case dataset unchanged.")


if __name__ == "__main__":
    main()
