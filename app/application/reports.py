import html
import re
from time import perf_counter
from uuid import uuid5

from app.domain.claim_ports import ClaimStore
from app.domain.llm import ModelUnavailable
from app.domain.reports import (
    FinalReport,
    ReportCitation,
    ReportFinding,
    ReportIssue,
    ReportStore,
    SynthesisOutput,
    SynthesisPort,
    SynthesisSection,
)
from app.domain.workflow import WorkflowError, WorkflowState
from app.domain.workflow_ports import WorkflowStore


class ReportBuilder:
    """Only persisted supported text can enter factual fields; all prose is controlled locally."""

    def __init__(
        self,
        claims: ClaimStore,
        store: WorkflowStore,
        reports: ReportStore,
        synthesizer: SynthesisPort,
    ):
        self._claims, self._store, self._reports, self._synthesizer = (
            claims,
            store,
            reports,
            synthesizer,
        )

    async def synthesize(self, state: WorkflowState):
        started = perf_counter()
        state = await self._store.save(
            state.evolve(execution_status="synthesizing", report_generation_status="running"),
            agent="synthesizer",
            event="synthesis_started",
        )
        collection = await self._claims.collection(state.run_id)
        report_id = uuid5(state.run_id, "final_report")
        findings, citations, issues = [], [], []
        for record in collection.claims:
            candidate = record.candidate
            result = record.attempts[-1].result if record.attempts else None
            if candidate.claim_id in collection.supported_claim_ids:
                current = {e.evidence_id: e for e in await self._claims.evidence_for(candidate)}
                local, seen = [], set()
                for quote in result.exact_supporting_quotes:
                    key = (quote.evidence_id, quote.start_char, quote.end_char, quote.text)
                    if key in seen:
                        continue
                    seen.add(key)
                    evidence = current[quote.evidence_id]
                    citation_id = uuid5(report_id, f"{candidate.claim_id}:{key}")
                    citations.append(
                        ReportCitation(
                            **quote.model_dump(),
                            citation_id=citation_id,
                            claim_id=candidate.claim_id,
                            verification_attempt_id=result.attempt_id,
                            original_url=evidence.original_url,
                            source_title=evidence.source_title,
                        )
                    )
                    local.append(citation_id)
                findings.append(
                    ReportFinding(
                        claim_id=candidate.claim_id,
                        claim_text=candidate.claim_text,
                        citation_ids=tuple(local),
                    )
                )
            else:
                category = record.verification_status
                if not result or result.outcome != "completed" or record.current_integrity_error:
                    category = "OPERATIONAL_ERROR"
                if category not in {"CONTRADICTED", "CONFLICTING", "INSUFFICIENT_EVIDENCE"}:
                    category = "OPERATIONAL_ERROR"
                issues.append(
                    ReportIssue(
                        claim_id=candidate.claim_id,
                        candidate_text=candidate.claim_text,
                        category=category,
                        explanation=result.explanation if result else "Verification not completed.",
                        evidence_ids=result.evidence_ids if result else (),
                        quotations=result.exact_supporting_quotes if result else (),
                    )
                )
        ids = tuple(f.claim_id for f in findings)
        fallback = SynthesisOutput(
            executive_summary_claim_ids=ids[:3],
            sections=(SynthesisSection(heading="Key findings", claim_ids=ids),) if ids else (),
        )
        output, model, code = fallback, None, None
        if findings and state.status != "failed":
            try:
                output, model = await self._synthesizer.organize(
                    state.run_id, state.original_question, tuple(findings)
                )
            except (WorkflowError, ModelUnavailable) as error:
                code = error.code
        generation = "model_organized" if model else "deterministic_incomplete"
        errors = tuple(
            sorted(
                set(
                    (
                        *state.errors,
                        *((code,) if code else ()),
                        *(
                            record.current_integrity_error
                            for record in collection.claims
                            if record.current_integrity_error
                        ),
                    )
                )
            )
        )
        if state.status == "failed":
            completion = "failed"
        elif any(
            "budget_exhausted" in item
            for item in (*errors, *(gap.stop_reason or "" for gap in state.research_gaps))
        ):
            completion = "budget_exhausted"
        elif code:
            completion = "partial" if findings else "failed"
        elif state.research_gaps or issues or errors:
            completion = "partial" if findings else "insufficient_evidence"
        else:
            completion = "complete" if findings else "insufficient_evidence"
        limitations = [
            "Verification judges supplied evidence support, not truth across all sources.",
            "Retained source text and research budgets are bounded; coverage is not exhaustive.",
            "Source text is untrusted; live accuracy and injection resistance remain unproven.",
        ]
        if state.mode != "live":
            limitations.append("This is an offline run, not live web research.")
        if generation != "model_organized":
            limitations.append(
                "Model synthesis did not complete; only deterministic verified text is shown."
            )
        counts = {}
        for record in collection.claims:
            counts[record.verification_status] = counts.get(record.verification_status, 0) + 1
        by_id = {f.claim_id: f for f in findings}
        report = FinalReport(
            report_id=report_id,
            run_id=state.run_id,
            research_question=state.original_question,
            generation_status=generation,
            completion_status=completion,
            mode=state.mode,
            executive_summary=tuple(by_id[c] for c in output.executive_summary_claim_ids),
            sections=output.sections,
            verified_findings=tuple(findings),
            supporting_citations=tuple(citations),
            evidence_limitations=tuple(limitations),
            unresolved_research_gaps=state.research_gaps,
            source_disagreements=tuple(i for i in issues if i.category == "CONFLICTING"),
            rejected_claims=tuple(i for i in issues if i.category == "CONTRADICTED"),
            insufficient_claims=tuple(
                i for i in issues if i.category in {"INSUFFICIENT_EVIDENCE", "OPERATIONAL_ERROR"}
            ),
            operational_errors=errors,
            methodology_summary="Configured method: Mesh planning, Tavily basic retrieval, "
            "exact stored-passage integrity checks, Mesh semantic verification and independent "
            "blind support review, bounded "
            "insufficient-only research and supported-claim organization. Inspect the audit "
            "for the stages and requests that actually executed.",
            verification_summary=counts,
            synthesizer_model=model,
        )
        # The next node validates citations and commits the report transaction.
        state = state.evolve(
            pending_report_json=report.model_dump_json(),
            synthesis_duration_seconds=round(perf_counter() - started, 6),
            errors=errors,
            supported_claim_ids=collection.supported_claim_ids,
            verification_counts=counts,
        )
        state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
        return await self._store.save(
            state,
            agent="synthesizer",
            event="synthesis_completed" if model else "synthesis_incomplete",
            details={
                "generation_status": generation,
                "error_code": code or "",
                "finding_count": len(findings),
            },
        )

    async def validate_report(self, state):
        started = perf_counter()
        report = FinalReport.model_validate_json(state.pending_report_json)
        state = await self._store.save(
            state.evolve(execution_status="validating_report"),
            agent="graph",
            event="report_validation_started",
        )
        try:
            await self._reports.save(report)
        except (WorkflowError, ValueError):
            state = state.evolve(
                status="failed",
                execution_status="failed",
                report_generation_status="failed",
                errors=(*state.errors, "report_integrity_failed"),
                pending_report_json=None,
            )
            return await self._store.save(
                state,
                agent="graph",
                event="report_rejected",
                details={"error_code": "report_integrity_failed"},
            )
        return await self._store.save(
            state.evolve(
                status="failed" if report.completion_status == "failed" else "completed",
                execution_status="report_ready",
                report_id=report.report_id,
                report_generation_status="ready"
                if report.completion_status == "complete"
                else "incomplete",
                report_validation_duration_seconds=round(perf_counter() - started, 6),
                pending_report_json=None,
            ),
            agent="graph",
            event="report_validated",
            details={
                "report_id": str(report.report_id),
                "finding_count": len(report.verified_findings),
                "citation_count": len(report.supporting_citations),
                "completion_status": report.completion_status,
            },
        )


def markdown_report(report: FinalReport) -> str:
    def safe(value):
        return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", html.escape(str(value)))

    citation_numbers = {c.citation_id: n for n, c in enumerate(report.supporting_citations, 1)}
    lines = [
        "# ProofTrail research report",
        "",
        "## Research question",
        safe(report.research_question),
        "",
        f"Completion: **{report.completion_status}**",
        f"Mode: {report.mode}",
        f"Generation: {report.generation_status}",
        "",
        "## Executive summary",
    ]

    def finding_line(finding):
        return (
            safe(finding.claim_text)
            + " "
            + " ".join(f"[C{citation_numbers[c]}]" for c in finding.citation_ids)
        )

    lines.extend(f"- {finding_line(f)}" for f in report.executive_summary)
    lines += ["", "## Verified findings"]
    by_id = {f.claim_id: f for f in report.verified_findings}
    for section in report.sections:
        lines += ["", f"### {section.heading}"]
        lines.extend(f"- {finding_line(by_id[c])}" for c in section.claim_ids)
    lines += ["", "## Supporting citations"]
    for n, c in enumerate(report.supporting_citations, 1):
        lines += [
            f"- C{n}: {safe(c.source_title)} — {safe(c.original_url)}",
            f"  Quote: {safe(c.text)}",
            f"  Evidence: {c.evidence_id}; SHA-256: {c.source_content_sha256}",
        ]
    for title, values in (
        ("Evidence limitations", report.evidence_limitations),
        (
            "Unresolved research gaps",
            [
                f"{g.kind}: {g.question}; {g.stop_reason or g.reason}"
                for g in report.unresolved_research_gaps
            ],
        ),
        (
            "Source disagreements (unresolved candidates)",
            [i.candidate_text for i in report.source_disagreements],
        ),
        (
            "Rejected candidates (not verified findings)",
            [i.candidate_text for i in report.rejected_claims],
        ),
        (
            "Insufficient candidates (not verified findings)",
            [i.candidate_text for i in report.insufficient_claims],
        ),
        ("Operational errors", report.operational_errors),
    ):
        lines += ["", f"## {title}"]
        lines.extend(f"- {safe(v)}" for v in values)
        if not values:
            lines.append("None recorded.")
    lines += [
        "",
        "## Research methodology",
        safe(report.methodology_summary),
        "",
        "## Verification summary",
        safe(report.verification_summary),
        "",
    ]
    return "\n".join(lines)
