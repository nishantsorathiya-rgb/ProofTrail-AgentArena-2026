"""Unverified candidates and auditable decisions; confidence never grants support."""

import json
from enum import StrEnum
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.domain.workflow import TypedRecord, utc_now


class Verdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICTING = "CONFLICTING"

    @property
    def locked_name(self) -> str:
        return {
            self.SUPPORTED: "supported",
            self.CONTRADICTED: "unsupported",
            self.INSUFFICIENT_EVIDENCE: "insufficient",
            self.CONFLICTING: "conflicting",
        }[self]


class DraftClaim(TypedRecord):
    claim_text: str = Field(min_length=1, max_length=1500)
    originating_research_task_id: UUID
    candidate_evidence_ids: tuple[UUID, ...] = Field(max_length=8)

    @field_validator("claim_text")
    @classmethod
    def original_text(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("A factual claim is required.")
        return value  # Preserve the model's original text, including whitespace.

    @field_validator("candidate_evidence_ids")
    @classmethod
    def unique_ids(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Repeated evidence identifiers are invalid.")
        return value


class DraftOutput(TypedRecord):
    claims: tuple[DraftClaim, ...] = Field(max_length=100)


class CandidateClaim(DraftClaim):
    claim_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)
    verification_status: Literal["UNVERIFIED"] = "UNVERIFIED"


class EvidenceInput(TypedRecord):
    evidence_id: UUID
    source_id: UUID
    original_url: str
    source_title: str
    source_content_sha256: str
    full_content_sha256: str
    text: str
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    locator: str


class ExactQuoteSelection(TypedRecord):
    """Model-selected text only; coordinates/provenance are never model-authored here."""

    evidence_id: UUID
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def meaningful_quote(cls, value):
        if not value.strip():
            raise ValueError("A quotation must contain evidence text.")
        return value


class ProposedQuote(ExactQuoteSelection):
    start_char: int = Field(ge=0)  # Absolute Python str offsets in retained source text.
    end_char: int = Field(gt=0)


class SemanticDecision(TypedRecord):
    claim_id: UUID
    verdict: Verdict
    evidence_ids: tuple[UUID, ...] = Field(max_length=8)
    exact_supporting_quotes: tuple[ProposedQuote, ...] = Field(max_length=16)
    explanation: str = Field(min_length=1, max_length=2000)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def citation_shape(self):
        if not self.explanation.strip() or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Invalid explanation or duplicate citations.")
        if self.verdict in {Verdict.SUPPORTED, Verdict.CONTRADICTED, Verdict.CONFLICTING}:
            if not self.evidence_ids or not self.exact_supporting_quotes:
                raise ValueError("Decisive verdicts require exact citations.")
        if {q.evidence_id for q in self.exact_supporting_quotes} != set(self.evidence_ids):
            raise ValueError("Each citation needs an exact quote, and vice versa.")
        if self.verdict == Verdict.CONFLICTING and len(self.evidence_ids) < 2:
            raise ValueError("Conflicts require at least two evidence passages.")
        return self


class QuoteSelectionDecision(SemanticDecision):
    """Explicit v2 wire contract. Legacy strict-coordinate decisions stay unchanged."""

    contract_version: Literal["exact_quotes_v2"]
    exact_supporting_quotes: tuple[ExactQuoteSelection, ...] = Field(max_length=16)


class VerifiedQuote(ProposedQuote):
    source_id: UUID
    source_content_sha256: str
    full_content_sha256: str
    locator: str


def evidence_fingerprint(evidence) -> str:
    content = [
        item.model_dump(mode="json") for item in sorted(evidence, key=lambda e: str(e.evidence_id))
    ]
    return sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SupportReviewReceipt(TypedRecord):
    policy: Literal["blind_semantic_support_review_v1"] = "blind_semantic_support_review_v1"
    primary_usage_id: UUID
    review_usage_id: UUID
    primary_decision: SemanticDecision
    review_decision: SemanticDecision
    review_model: str
    evidence_sha256: str
    reviewed_at: AwareDatetime = Field(default_factory=utc_now)

    def digest(self):
        return sha256(self.model_dump_json().encode()).hexdigest()


class VerificationResult(TypedRecord):
    attempt_id: UUID
    claim_id: UUID
    run_id: UUID
    verdict: Verdict
    locked_verdict: str
    evidence_ids: tuple[UUID, ...] = ()
    exact_supporting_quotes: tuple[VerifiedQuote, ...] = ()
    explanation: str
    confidence: float | None = None
    outcome: Literal["completed", "validation_rejected", "operational_error"]
    rejection_reason: str | None = None
    verification_timestamp: AwareDatetime = Field(default_factory=utc_now)
    verifier_model: str | None = None
    verification_duration_ms: float = 0
    persistence_duration_ms: float = 0
    llm_requests: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_input_tokens: int | None = None  # ceil(JSON characters / 4); not model tokenization.
    research_retry_id: UUID | None = None
    previous_verdict: str | None = None
    evaluated_evidence_ids: tuple[UUID, ...] = ()
    # Optional when reading historical JSON; usable support additionally requires the policy
    # receipt and its actual persisted model requests/audit event. Never auto-upgrade old results.
    support_review_receipt: SupportReviewReceipt | None = None
    primary_decision: SemanticDecision | None = None

    @model_validator(mode="after")
    def trustworthy_shape(self):
        if self.locked_verdict != self.verdict.locked_name:
            raise ValueError("Verdict mapping mismatch.")
        if self.outcome != "completed":
            if (
                self.verdict != Verdict.INSUFFICIENT_EVIDENCE
                or self.evidence_ids
                or self.exact_supporting_quotes
                or not self.rejection_reason
            ):
                raise ValueError("Failures cannot certify evidence support.")
        else:
            SemanticDecision(
                claim_id=self.claim_id,
                verdict=self.verdict,
                evidence_ids=self.evidence_ids,
                exact_supporting_quotes=tuple(
                    ProposedQuote(**{key: getattr(q, key) for key in ProposedQuote.model_fields})
                    for q in self.exact_supporting_quotes
                ),
                explanation=self.explanation,
                confidence=self.confidence,
            )
        return self


class VerificationAttempt(TypedRecord):
    attempt_id: UUID
    claim_id: UUID
    started_at: AwareDatetime
    status: Literal["pending", "finished", "interrupted"]
    result: VerificationResult | None = None
    evidence_scope_ids: tuple[UUID, ...] = ()
    research_retry_id: UUID | None = None


class ClaimRecord(TypedRecord):
    candidate: CandidateClaim
    verification_status: str = "UNVERIFIED"
    attempts: tuple[VerificationAttempt, ...] = ()
    current_integrity_error: str | None = None


class ClaimCollection(TypedRecord):
    run_id: UUID
    claims: tuple[ClaimRecord, ...]
    supported_claim_ids: tuple[UUID, ...] = ()
    note: str = "Evidence-support decisions and immutable verification history."
