"""Versioned, owner-reviewable evaluation data, isolated from runtime agent inputs."""

import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.domain.claims import Verdict
from app.domain.urls import normalize_url
from app.domain.workflow import TypedRecord


class EvaluationCase(TypedRecord):
    case_id: str = Field(min_length=1, max_length=100)
    claim_text: str = Field(min_length=1, max_length=1500)
    evidence_text: str = Field(min_length=1, max_length=12000)
    evidence_source: str = Field(min_length=1)
    evidence_provenance: str = Field(min_length=1)
    expected_verdict: Verdict
    explanation: str = Field(min_length=1)
    review_status: Literal["UNREVIEWED", "HUMAN_REVIEWED", "AMBIGUOUS", "EXCLUDED"]
    reviewer_notes: str = ""
    reviewer: str = ""
    exclusion_reason: str = ""
    dataset_split: Literal["development", "held_out", "adversarial"]
    evidence_kind: Literal["synthetic", "authentic_snapshot"]
    family_id: str = Field(min_length=1)
    challenge: str = "controlled_record"

    @field_validator("claim_text", "evidence_text", "evidence_source", "evidence_provenance")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Evaluation text/provenance cannot be blank.")
        return value

    @model_validator(mode="after")
    def independent_review(self):
        if self.review_status == "HUMAN_REVIEWED" and not self.reviewer.strip():
            raise ValueError("Human review requires a named reviewer.")
        if self.review_status == "EXCLUDED" and not self.exclusion_reason.strip():
            raise ValueError("Exclusions require a documented reason.")
        if self.evidence_kind == "synthetic" and not self.evidence_source.startswith("fixture:"):
            raise ValueError("Synthetic evidence must have an explicit fixture identity.")
        if self.evidence_kind == "authentic_snapshot":
            normalize_url(self.evidence_source)
        return self


class EvaluationDataset(TypedRecord):
    dataset_version: str = Field(min_length=1)
    construction: str = Field(min_length=1)
    cases: tuple[EvaluationCase, ...]

    @model_validator(mode="after")
    def unique_and_separate(self):
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate evaluation case IDs.")
        families = {}
        evidence_splits = {}
        for case in self.cases:
            for key, registry in (
                (case.family_id, families),
                (case.evidence_text, evidence_splits),
            ):
                if key in registry and registry[key] != case.dataset_split:
                    raise ValueError("Evidence families must not cross dataset splits.")
                registry[key] = case.dataset_split
        return self

    def validate_target(self):
        expected = {
            (split, verdict): count
            for split, count in (("development", 12), ("held_out", 8))
            for verdict in (Verdict.SUPPORTED, Verdict.CONTRADICTED, Verdict.INSUFFICIENT_EVIDENCE)
        }
        if Counter((c.dataset_split, c.expected_verdict) for c in self.cases) != expected:
            raise ValueError(
                "Target corpus requires 60 cases, balanced 36 development/24 held-out."
            )


def load_dataset(path: Path) -> EvaluationDataset:
    return EvaluationDataset.model_validate_json(path.read_text(encoding="utf-8"))


def dataset_digest(dataset: EvaluationDataset) -> str:
    canonical = json.dumps(dataset.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


class AdversarialScenario(TypedRecord):
    case: EvaluationCase
    provider_mode: Literal[
        "normal",
        "false_support",
        "fabricated_reference",
        "bad_quote",
        "wrong_claim",
        "wrong_offset",
        "timeout",
        "malformed",
    ]
    mutation: Literal["changed_source", "invalid_candidate_id", "budget_exhausted"] | None = None


class AdversarialDataset(TypedRecord):
    dataset_version: str
    scenarios: tuple[AdversarialScenario, ...]
