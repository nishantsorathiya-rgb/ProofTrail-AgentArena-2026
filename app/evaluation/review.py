"""Explicit owner edits are imported; no code assigns HUMAN_REVIEWED or guesses reviewers."""

import csv
from pathlib import Path

from app.evaluation.dataset import EvaluationCase, EvaluationDataset

EDITABLE = {
    "expected_verdict",
    "explanation",
    "review_status",
    "reviewer_notes",
    "reviewer",
    "exclusion_reason",
}


def apply_owner_review(dataset: EvaluationDataset, worksheet: Path) -> EvaluationDataset:
    with worksheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    by_id = {row["case_id"]: row for row in rows}
    if len(by_id) != len(rows) or set(by_id) != {c.case_id for c in dataset.cases}:
        raise ValueError(
            "Worksheet IDs must match the versioned dataset exactly, without duplicates."
        )
    cases = []
    for original in dataset.cases:
        row = by_id[original.case_id]
        baseline = original.model_dump(mode="json")
        if set(row) != set(baseline) or any(
            row[k] != str(baseline[k]) for k in baseline if k not in EDITABLE
        ):
            raise ValueError(
                "Immutable evidence/split fields changed; create a new dataset version."
            )
        cases.append(EvaluationCase.model_validate(row))
    return EvaluationDataset(
        dataset_version=dataset.dataset_version + "+owner-worksheet",
        construction=dataset.construction,
        cases=tuple(cases),
    )
