import csv
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation.dataset import EvaluationCase, EvaluationDataset, dataset_digest, load_dataset
from app.evaluation.review import apply_owner_review
from scripts.evaluate_p6 import evaluate

DATA = Path(__file__).parent / "datasets"


def test_sixty_explicit_synthetic_unreviewed_cases_have_balanced_separate_families():
    dataset = load_dataset(DATA / "claims.v1.json")
    dataset.validate_target()
    assert len(dataset.cases) == 60
    assert sorted(Counter(c.expected_verdict for c in dataset.cases).values()) == [20, 20, 20]
    assert all(
        c.review_status == "UNREVIEWED"
        and c.evidence_kind == "synthetic"
        and not c.reviewer
        and c.evidence_source.startswith("fixture:")
        for c in dataset.cases
    )
    assert dataset_digest(dataset) == dataset_digest(load_dataset(DATA / "claims.v1.json"))
    dev = {c.family_id for c in dataset.cases if c.dataset_split == "development"}
    held = {c.family_id for c in dataset.cases if c.dataset_split == "held_out"}
    assert not (dev & held)


def test_invalid_schema_duplicates_and_cross_split_families_fail_safely():
    dataset = load_dataset(DATA / "claims.v1.json")
    original = dataset.cases[0].model_dump()
    with pytest.raises(ValidationError):
        EvaluationCase.model_validate({**original, "unknown": "extra"})
    with pytest.raises(ValidationError, match="reviewer"):
        EvaluationCase.model_validate({**original, "review_status": "HUMAN_REVIEWED"})
    with pytest.raises(ValidationError, match="reason"):
        EvaluationCase.model_validate({**original, "review_status": "EXCLUDED"})
    for bad in (
        (dataset.cases[0], dataset.cases[0]),
        (
            dataset.cases[0],
            EvaluationCase.model_validate(
                {**original, "case_id": "other", "dataset_split": "held_out"}
            ),
        ),
    ):
        with pytest.raises(ValidationError):
            EvaluationDataset(dataset_version="bad", construction="invalid", cases=bad)


def worksheet(tmp_path, mutate):
    with (DATA / "owner_review.v1.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    mutate(rows)
    path = tmp_path / "review.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(EvaluationCase.model_fields))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_owner_corrections_are_explicit_and_evidence_split_cannot_be_overwritten(tmp_path):
    dataset = load_dataset(DATA / "claims.v1.json")

    def review(rows):
        rows[0].update(
            review_status="HUMAN_REVIEWED",
            reviewer="TEST ONLY owner action",
            expected_verdict="INSUFFICIENT_EVIDENCE",
            reviewer_notes="Test correction",
        )
        rows[1].update(review_status="EXCLUDED", exclusion_reason="Test ambiguity")

    reviewed = apply_owner_review(dataset, worksheet(tmp_path, review))
    assert reviewed.cases[0].review_status == "HUMAN_REVIEWED"
    assert reviewed.cases[0].expected_verdict.value == "INSUFFICIENT_EVIDENCE"
    assert dataset.cases[0].review_status == "UNREVIEWED"  # Original is untouched.
    assert reviewed.cases[1].exclusion_reason == "Test ambiguity"
    assert all(c.review_status == "UNREVIEWED" for c in reviewed.cases[2:])
    bad = worksheet(tmp_path, lambda rows: rows[0].update(dataset_split="held_out"))
    with pytest.raises(ValueError, match="Immutable"):
        apply_owner_review(dataset, bad)


async def test_held_out_is_not_executed_accidentally():
    with pytest.raises(ValueError, match="allow-held-out"):
        await evaluate(split="held_out", include_workflows=False)
