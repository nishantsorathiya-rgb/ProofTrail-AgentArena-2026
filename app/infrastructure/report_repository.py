import json
from hashlib import sha256
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, text

from app.domain.claims import CandidateClaim, Verdict, VerificationResult
from app.domain.reports import FinalReport
from app.domain.workflow import WorkflowError
from app.infrastructure.claim_models import ClaimRow, VerificationRow
from app.infrastructure.claim_repository import SQLiteClaimRepository
from app.infrastructure.database import Database, ResearchRunRow
from app.infrastructure.report_models import ReportRow


class SQLiteReportRepository:
    def __init__(self, database: Database, claims: SQLiteClaimRepository):
        self._sessions, self._claims = database.sessions, claims

    async def _validate(self, session, report):
        run = await session.get(ResearchRunRow, str(report.run_id))
        if run is None or run.question != report.research_question:
            raise WorkflowError("report_question_mismatch")
        citations = {c.citation_id: c for c in report.supporting_citations}
        for finding in report.verified_findings:
            row = await session.get(ClaimRow, str(finding.claim_id))
            if (
                row is None
                or row.run_id != str(report.run_id)
                or row.claim_text != finding.claim_text
                or row.verification_status != "SUPPORTED"
            ):
                raise WorkflowError("report_claim_not_supported")
            candidate = CandidateClaim.model_validate_json(row.candidate_json)
            attempt = await session.scalar(
                select(VerificationRow)
                .where(VerificationRow.claim_id == row.claim_id)
                .order_by(VerificationRow.sequence.desc())
                .limit(1)
            )
            if attempt is None or attempt.status != "finished" or attempt.result_json is None:
                raise WorkflowError("report_verification_missing")
            result = VerificationResult.model_validate_json(attempt.result_json)
            if result.verdict != Verdict.SUPPORTED or result.outcome != "completed":
                raise WorkflowError("report_claim_not_supported")
            scope = tuple(UUID(p) for p in json.loads(attempt.evidence_scope_json))
            current = {
                p.evidence_id: p
                for p in await self._claims._evidence(
                    session, candidate, scope or candidate.candidate_evidence_ids
                )
            }
            self._claims._check_result(candidate, result, current)
            await self._claims._check_support_receipt(session, candidate, result)
            for citation_id in finding.citation_ids:
                citation = citations[citation_id]
                evidence = current.get(citation.evidence_id)
                quote_fields = {
                    k: getattr(citation, k)
                    for k in type(result.exact_supporting_quotes[0]).model_fields
                }
                if (
                    evidence is None
                    or citation.verification_attempt_id != result.attempt_id
                    or citation.original_url != evidence.original_url
                    or citation.source_title != evidence.source_title
                    or not any(
                        q.model_dump() == quote_fields for q in result.exact_supporting_quotes
                    )
                ):
                    raise WorkflowError("report_citation_invalid")

    async def save(self, report: FinalReport):
        report = FinalReport.model_validate(report.model_dump())
        async with self._sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            await self._validate(session, report)
            content = report.model_dump_json()
            previous = await session.get(ReportRow, str(report.run_id))
            if previous:
                if previous.report_json != content:
                    raise WorkflowError("report_history_rewrite_rejected")
                return
            session.add(
                ReportRow(
                    run_id=str(report.run_id),
                    report_id=str(report.report_id),
                    generated_at=report.generated_at,
                    report_json=content,
                    content_sha256=sha256(content.encode()).hexdigest(),
                )
            )

    async def get(self, run_id):
        async with self._sessions() as session:
            row = await session.get(ReportRow, str(run_id))
            if row is None:
                return None
            if sha256(row.report_json.encode()).hexdigest() != row.content_sha256:
                raise WorkflowError("report_integrity_failed")
            try:
                report = FinalReport.model_validate_json(row.report_json)
                await self._validate(session, report)
            except (WorkflowError, ValidationError, ValueError, KeyError):
                raise WorkflowError("report_integrity_failed") from None
            return report

    async def validate(self, report):
        async with self._sessions() as session:
            await self._validate(session, FinalReport.model_validate(report.model_dump()))
