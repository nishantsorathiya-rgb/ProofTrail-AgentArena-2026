"""Closed synthesis vocabulary prevents new model-written factual prose entering reports."""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.domain.claims import VerifiedQuote
from app.domain.workflow import ResearchGap, TypedRecord, utc_now

Heading = Literal["Key findings", "Quantitative findings", "Timeline", "Additional findings"]


class SynthesisSection(TypedRecord):
    heading: Heading
    claim_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)


class SynthesisOutput(TypedRecord):
    executive_summary_claim_ids: tuple[UUID, ...] = Field(max_length=100)
    sections: tuple[SynthesisSection, ...] = Field(max_length=4)


class ReportCitation(VerifiedQuote):
    citation_id: UUID
    claim_id: UUID
    verification_attempt_id: UUID
    original_url: str
    source_title: str


class ReportFinding(TypedRecord):
    claim_id: UUID
    claim_text: str
    citation_ids: tuple[UUID, ...] = Field(min_length=1)


class ReportIssue(TypedRecord):
    claim_id: UUID
    candidate_text: str
    category: Literal["CONTRADICTED", "INSUFFICIENT_EVIDENCE", "CONFLICTING", "OPERATIONAL_ERROR"]
    explanation: str
    evidence_ids: tuple[UUID, ...] = ()
    quotations: tuple[VerifiedQuote, ...] = ()


class FinalReport(TypedRecord):
    report_id: UUID
    run_id: UUID
    research_question: str
    generated_at: AwareDatetime = Field(default_factory=utc_now)
    generation_status: Literal["model_organized", "deterministic_incomplete"]
    completion_status: Literal[
        "complete", "partial", "insufficient_evidence", "budget_exhausted", "failed", "interrupted"
    ]
    mode: Literal["offline_disabled", "offline_fixture", "live"]
    executive_summary: tuple[ReportFinding, ...]
    verified_findings: tuple[ReportFinding, ...]
    sections: tuple[SynthesisSection, ...]
    supporting_citations: tuple[ReportCitation, ...]
    evidence_limitations: tuple[str, ...]
    unresolved_research_gaps: tuple[ResearchGap, ...]
    source_disagreements: tuple[ReportIssue, ...]
    rejected_claims: tuple[ReportIssue, ...]
    insufficient_claims: tuple[ReportIssue, ...]
    operational_errors: tuple[str, ...]
    methodology_summary: str
    verification_summary: dict[str, int]
    synthesizer_model: str | None = None
    citation_validation_status: Literal["valid"] = "valid"

    @model_validator(mode="after")
    def closed_factual_content(self):
        findings = {f.claim_id: f for f in self.verified_findings}
        if len(findings) != len(self.verified_findings):
            raise ValueError("Duplicate report claim.")
        if any(
            f.claim_id not in findings or f != findings[f.claim_id] for f in self.executive_summary
        ):
            raise ValueError("Summary must reuse exact verified findings.")
        selected = [claim for section in self.sections for claim in section.claim_ids]
        if set(selected) != set(findings) or len(selected) != len(findings):
            raise ValueError("Sections must organize every finding exactly once.")
        citations = {c.citation_id: c for c in self.supporting_citations}
        if len(citations) != len(self.supporting_citations):
            raise ValueError("Duplicate citation.")
        referenced = set()
        for finding in findings.values():
            for citation_id in finding.citation_ids:
                if (
                    citation_id not in citations
                    or citations[citation_id].claim_id != finding.claim_id
                ):
                    raise ValueError("Citation must reference its exact claim.")
                referenced.add(citation_id)
        if referenced != set(citations):
            raise ValueError("Unassigned citation.")
        if {
            i.claim_id
            for i in (*self.rejected_claims, *self.insufficient_claims, *self.source_disagreements)
        } & set(findings):
            raise ValueError("Unverified or rejected claim cannot enter findings.")
        return self


class ReportStore(Protocol):
    async def save(self, report: FinalReport) -> None: ...

    async def get(self, run_id: UUID) -> FinalReport | None: ...

    async def validate(self, report: FinalReport) -> None: ...


class SynthesisPort(Protocol):
    async def organize(
        self, run_id: UUID, question: str, findings: tuple[ReportFinding, ...]
    ) -> tuple[SynthesisOutput, str]: ...
