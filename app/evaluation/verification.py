import csv
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from app.domain.claims import CandidateClaim, Verdict, VerificationResult
from app.domain.workflow import TypedRecord

LABELS = (Verdict.SUPPORTED, Verdict.CONTRADICTED, Verdict.INSUFFICIENT_EVIDENCE)


class VerificationEvaluationPort(Protocol):
    async def verify(self, claim: CandidateClaim) -> VerificationResult: ...


class ReviewedCase(TypedRecord):
    case_id: str
    expected: Verdict
    split: Literal["dev", "held_out"]
    source_type: str
    source_reference: str
    exact_evidence_passage: str
    atomic_claim: str
    human_reviewer: str


def reviewed_cases(path: Path) -> tuple[ReviewedCase, ...]:
    """Only explicitly APPROVED, populated rows qualify; templates never become ground truth."""
    mapping = {
        "supported": Verdict.SUPPORTED,
        "contradicted": Verdict.CONTRADICTED,
        "unsupported": Verdict.CONTRADICTED,
        "insufficient": Verdict.INSUFFICIENT_EVIDENCE,
    }
    cases = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["review_status"] != "APPROVED":
                continue
            fields = (
                "case_id",
                "source_type",
                "source_url_or_fixture_id",
                "exact_evidence_passage",
                "atomic_claim",
                "human_reviewer",
            )
            if any(not row[key].strip() for key in fields):
                raise ValueError("Approved cases must be complete and have a human reviewer.")
            if row["expected_label"] not in mapping:
                raise ValueError("Three-class corpus only; conflicts use a separate suite.")
            cases.append(
                ReviewedCase(
                    case_id=row["case_id"],
                    expected=mapping[row["expected_label"]],
                    split=row["split"],
                    source_type=row["source_type"],
                    source_reference=row["source_url_or_fixture_id"],
                    exact_evidence_passage=row["exact_evidence_passage"],
                    atomic_claim=row["atomic_claim"],
                    human_reviewer=row["human_reviewer"],
                )
            )
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Duplicate reviewed case IDs.")
    return tuple(cases)


class EvaluationObservation(TypedRecord):
    case_id: str
    expected: Verdict
    predicted: Verdict
    emitted_citations: int = Field(ge=0)
    valid_citations: int = Field(ge=0)
    operational_error: bool = False
    validation_rejected: bool = False


def metrics(observations: tuple[EvaluationObservation, ...]) -> dict:
    if any(
        o.expected not in LABELS or o.valid_citations > o.emitted_citations for o in observations
    ):
        raise ValueError("Invalid evaluation observation; conflicts need their own ground truth.")
    if len({o.case_id for o in observations}) != len(observations):
        raise ValueError("Duplicate evaluation observations.")
    columns = (
        *(v.value for v in LABELS),
        "CONFLICTING",
        "OPERATIONAL_ERROR",
        "VALIDATION_REJECTED",
    )
    matrix = {label.value: dict.fromkeys(columns, 0) for label in LABELS}
    for row in observations:
        column = (
            "OPERATIONAL_ERROR"
            if row.operational_error
            else "VALIDATION_REJECTED"
            if row.validation_rejected
            else row.predicted.value
        )
        matrix[row.expected.value][column] += 1

    def ratio(numerator, denominator):
        return numerator / denominator if denominator else None

    per_class = {}
    for label in LABELS:
        tp = matrix[label.value][label.value]
        actual = sum(matrix[label.value].values())
        emitted = sum(matrix[row.value][label.value] for row in LABELS)
        precision, recall = ratio(tp, emitted), ratio(tp, actual)
        per_class[label.value] = {
            "precision": precision,
            "recall": recall,
            "actual_count": actual,
            "predicted_count": emitted,
            "f1": (2 * precision * recall / (precision + recall) if precision + recall else 0.0)
            if precision is not None and recall is not None
            else None,
        }
    contradicted = [o for o in observations if o.expected == Verdict.CONTRADICTED]
    non_supported = [o for o in observations if o.expected != Verdict.SUPPORTED]
    emitted = sum(o.emitted_citations for o in observations)
    valid = sum(o.valid_citations for o in observations)
    false_supported = sum(
        not o.operational_error
        and not o.validation_rejected
        and o.predicted == Verdict.SUPPORTED
        and o.expected != Verdict.SUPPORTED
        for o in observations
    )
    f1s = [entry["f1"] for entry in per_class.values()]
    return {
        "case_count": len(observations),
        "confusion_matrix": matrix,
        "classification_accuracy": ratio(
            sum(
                not o.operational_error and not o.validation_rejected and o.expected == o.predicted
                for o in observations
            ),
            len(observations),
        ),
        "per_class": per_class,
        "macro_f1": sum(f1s) / len(LABELS) if all(f is not None for f in f1s) else None,
        "false_supported_verdicts": false_supported,
        "false_supported_rate": ratio(false_supported, len(non_supported)),
        "false_supported_rate_denominator": len(non_supported),
        "supported_assertion_precision": per_class["SUPPORTED"]["precision"],
        "unsupported_claim_rejection": ratio(
            sum(
                not o.operational_error
                and not o.validation_rejected
                and o.predicted != Verdict.SUPPORTED
                for o in non_supported
            ),
            len(non_supported),
        ),
        "contradicted_rejection_recall": ratio(
            sum(
                not o.operational_error
                and not o.validation_rejected
                and o.predicted == Verdict.CONTRADICTED
                for o in contradicted
            ),
            len(contradicted),
        ),
        "citation_validity": ratio(valid, emitted),
        "valid_citations": valid,
        "emitted_citations": emitted,
        "invalid_citations": emitted - valid,
        "operational_errors": sum(o.operational_error for o in observations),
        "validation_rejections": sum(o.validation_rejected for o in observations),
        "undefined_denominators": "null means N/A, never 100%",
    }
