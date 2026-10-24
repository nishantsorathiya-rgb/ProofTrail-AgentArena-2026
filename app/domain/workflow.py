"""Typed collection and verification state; historical P3 snapshots remain readable."""

import re
import unicodedata
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.domain.retrieval import EvidencePassage, SourceSnapshot


def utc_now() -> datetime:
    return datetime.now(UTC)


def task_key(text: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


class WorkflowError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TypedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Subquestion(TypedRecord):
    question: str = Field(min_length=1, max_length=600)
    search_query: str = Field(min_length=1, max_length=400)
    evidence_requirement: str = Field(min_length=1, max_length=600)

    @field_validator("question", "search_query", "evidence_requirement")
    @classmethod
    def meaningful_text(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Nonblank text without null characters is required.")
        return value.strip()


class ResearchPlan(TypedRecord):
    goal: str = Field(min_length=1, max_length=1000)
    subquestions: tuple[Subquestion, ...] = Field(min_length=1, max_length=10)

    @field_validator("goal")
    @classmethod
    def nonblank_goal(cls, value):
        if not value.strip():
            raise ValueError("A focused goal is required.")
        return value.strip()


class BudgetState(TypedRecord):
    max_llm_calls: int = Field(ge=1)
    max_searches: int = Field(ge=1)
    max_fetches: int = Field(ge=1)
    model_attempts: int = 0
    llm_calls: int = 0
    search_requests: int = 0
    extract_requests: int = 0
    fetch_units: int = 0
    max_research_tasks: int = 9
    max_retries_per_subquestion: int = Field(default=1, ge=0, le=2)
    max_claims: int = 18


class ResearchTask(TypedRecord):
    task_id: UUID
    assignment: Subquestion
    parent_task_id: UUID | None = None
    retry_number: int = Field(default=0, ge=0, le=2)
    status: Literal[
        "queued",
        "running",
        "collected",
        "partial",
        "empty",
        "failed",
        "budget_exhausted",
        "interrupted",
    ] = "queued"
    source_ids: tuple[UUID, ...] = ()
    passage_ids: tuple[UUID, ...] = ()
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_seconds: float = 0


class SourceReference(TypedRecord):
    source_id: UUID
    original_url: str
    title: str
    retrieved_at: AwareDatetime


class AuditEvent(TypedRecord):
    event_id: UUID = Field(default_factory=uuid4)
    sequence: int
    timestamp: AwareDatetime = Field(default_factory=utc_now)
    agent: Literal[
        "coordinator", "planner", "researcher", "drafter", "verifier", "synthesizer", "graph"
    ]
    event: str
    task_id: UUID | None = None
    details: dict[str, str | int | float | list[str]] = Field(default_factory=dict)


class ResearchGap(TypedRecord):
    gap_id: UUID
    root_task_id: UUID
    kind: Literal[
        "missing_evidence",
        "incomplete_coverage",
        "insufficient_support",
        "contradicted",
        "conflicting",
        "operational_error",
    ]
    question: str
    claim_ids: tuple[UUID, ...] = ()
    reason: str
    retryable: bool = False
    stop_reason: str | None = None


class ResearchRetry(TypedRecord):
    retry_id: UUID
    root_task_id: UUID
    task_id: UUID
    number: int = Field(ge=1, le=2)
    trigger: str
    question: str
    search_query: str
    claim_ids: tuple[UUID, ...] = ()
    drafted_claim_ids: tuple[UUID, ...] = ()
    previous_verdicts: dict[str, str] = Field(default_factory=dict)
    updated_verdicts: dict[str, str] = Field(default_factory=dict)
    previous_evidence_ids: tuple[UUID, ...] = ()
    new_evidence_ids: tuple[UUID, ...] = ()
    status: Literal[
        "queued",
        "researching",
        "collected",
        "completed",
        "no_new_evidence",
        "failed",
        "interrupted",
    ] = "queued"
    created_at: AwareDatetime = Field(default_factory=utc_now)
    finished_at: AwareDatetime | None = None
    duration_seconds: float = 0
    error_code: str | None = None


class WorkflowState(TypedRecord):
    run_id: UUID
    original_question: str = Field(min_length=1, max_length=4000)
    phase: Literal["P3", "P4", "P5"] = "P3"
    verification_status: Literal[
        "not_implemented", "pending", "running", "completed", "partial", "failed", "not_run"
    ] = "not_implemented"
    mode: Literal["offline_disabled", "offline_fixture", "live"]
    status: Literal["queued", "running", "completed", "failed", "interrupted"] = "queued"
    execution_status: Literal[
        "queued",
        "planning",
        "researching",
        "drafting",
        "verifying",
        "verification_completed",
        "deciding",
        "planning_retry",
        "retrying",
        "reverifying",
        "synthesizing",
        "validating_report",
        "report_ready",
        "evidence_collected",
        "partial",
        "no_evidence",
        "budget_exhausted",
        "failed",
        "interrupted",
    ] = "queued"
    research_plan: ResearchPlan | None = None
    subquestions: tuple[ResearchTask, ...] = ()
    research_task_status: dict[str, str] = Field(default_factory=dict)
    retrieved_evidence_ids: tuple[UUID, ...] = ()
    source_references: tuple[SourceReference, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    budgets: BudgetState
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    planning_duration_seconds: float = 0
    research_duration_seconds: float = 0
    total_duration_seconds: float = 0
    cache_hits: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    collection_status: str | None = None
    candidate_claim_ids: tuple[UUID, ...] = ()
    supported_claim_ids: tuple[UUID, ...] = ()
    verification_counts: dict[str, int] = Field(default_factory=dict)
    drafting_duration_seconds: float = 0
    verification_duration_seconds: float = 0
    verification_persistence_duration_ms: float = 0
    verification_llm_requests: int = 0
    verification_failures: int = 0
    verification_prompt_tokens: int | None = None
    verification_completion_tokens: int | None = None
    research_gaps: tuple[ResearchGap, ...] = ()
    retry_history: tuple[ResearchRetry, ...] = ()
    retry_counts: dict[str, int] = Field(default_factory=dict)
    active_retry_ids: tuple[UUID, ...] = ()
    eligible_gap_ids: tuple[UUID, ...] = ()
    correction_stopped: bool = False
    correction_stop_reason: str | None = None
    retry_duration_seconds: float = 0
    synthesis_duration_seconds: float = 0
    report_validation_duration_seconds: float = 0
    report_id: UUID | None = None
    pending_report_json: str | None = Field(default=None, repr=False)
    report_generation_status: Literal[
        "not_implemented", "pending", "running", "ready", "incomplete", "failed"
    ] = "not_implemented"
    audit_events: tuple[AuditEvent, ...] = ()

    def evolve(self, **updates) -> "WorkflowState":
        values = {name: getattr(self, name) for name in type(self).model_fields}
        return type(self).model_validate({**values, **updates})


class CollectedEvidence(TypedRecord):
    run_id: UUID
    verification_status: Literal["not_implemented", "unverified_source_material"] = (
        "not_implemented"
    )
    trust_level: Literal["untrusted_source_text"] = "untrusted_source_text"
    sources: tuple[SourceSnapshot, ...] = ()
    passages: tuple[EvidencePassage, ...] = ()
