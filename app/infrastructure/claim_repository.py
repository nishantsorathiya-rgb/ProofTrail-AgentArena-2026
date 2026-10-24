import json
import re
from hashlib import sha256
from time import perf_counter
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert

from app.domain.claims import (
    CandidateClaim,
    ClaimCollection,
    ClaimRecord,
    EvidenceInput,
    Verdict,
    VerificationAttempt,
    VerificationResult,
    evidence_fingerprint,
)
from app.domain.retrieval import InvalidSourceURL
from app.domain.urls import normalize_url
from app.domain.workflow import WorkflowError, utc_now
from app.infrastructure.claim_models import ClaimEvidenceRow, ClaimRow, VerificationRow
from app.infrastructure.database import Database
from app.infrastructure.evidence_models import ExtractionRow, PassageRow, SourceRow
from app.infrastructure.report_models import RetryRow
from app.infrastructure.workflow_models import AuditRow, ModelUsageRow, SubquestionRow


class SQLiteClaimRepository:
    def __init__(self, database: Database, max_claims: int = 18, max_attempts: int = 2):
        self._sessions, self._max_claims = database.sessions, max_claims
        self._max_attempts = max_attempts

    async def _scope(self, session, claim):
        linked = (
            await session.scalars(
                select(ClaimEvidenceRow.passage_id)
                .where(ClaimEvidenceRow.claim_id == str(claim.claim_id))
                .order_by(ClaimEvidenceRow.passage_id)
            )
        ).all()
        return tuple(dict.fromkeys((*claim.candidate_evidence_ids, *(UUID(p) for p in linked))))

    @staticmethod
    def _check_result(claim, result, current):
        if result.claim_id != claim.claim_id or result.run_id != claim.run_id:
            raise WorkflowError("verification_history_mismatch")
        for quote in result.exact_supporting_quotes:
            item = current.get(quote.evidence_id)
            if (
                item is None
                or item.source_id != quote.source_id
                or item.source_content_sha256 != quote.source_content_sha256
                or item.full_content_sha256 != quote.full_content_sha256
                or quote.start_char < item.start_char
                or quote.end_char > item.end_char
                or quote.end_char <= quote.start_char
                or item.text[quote.start_char - item.start_char : quote.end_char - item.start_char]
                != quote.text
            ):
                raise WorkflowError("evidence_changed_before_persistence")
        if result.verdict == Verdict.SUPPORTED:
            receipt = result.support_review_receipt
            if receipt is None:
                raise WorkflowError("semantic_support_review_missing")
            review = receipt.review_decision
            quotes = tuple(
                {k: getattr(q, k) for k in ("evidence_id", "text", "start_char", "end_char")}
                for q in result.exact_supporting_quotes
            )
            if (
                receipt.primary_usage_id == receipt.review_usage_id
                or result.llm_requests < 2
                or result.primary_decision != receipt.primary_decision
                or receipt.primary_decision.claim_id != claim.claim_id
                or receipt.primary_decision.verdict != Verdict.SUPPORTED
                or review.claim_id != claim.claim_id
                or review.verdict != Verdict.SUPPORTED
                or review.evidence_ids != result.evidence_ids
                or tuple(q.model_dump() for q in review.exact_supporting_quotes) != quotes
                or receipt.evidence_sha256 != evidence_fingerprint(tuple(current.values()))
            ):
                raise WorkflowError("semantic_support_review_invalid")

    @staticmethod
    async def _check_support_receipt(session, claim, result):
        if result.verdict != Verdict.SUPPORTED:
            return
        receipt = result.support_review_receipt
        if receipt is None:
            raise WorkflowError("semantic_support_review_missing")
        for usage_id in (receipt.primary_usage_id, receipt.review_usage_id):
            usage = await session.get(ModelUsageRow, str(usage_id))
            if (
                not usage
                or usage.run_id != str(claim.run_id)
                or usage.status != "success"
                or not usage.outbound_called
                or usage.model
                != (
                    receipt.review_model
                    if usage_id == receipt.review_usage_id
                    else result.verifier_model
                )
            ):
                raise WorkflowError("semantic_support_review_usage_invalid")
        events = (
            await session.scalars(
                select(AuditRow).where(
                    AuditRow.run_id == str(claim.run_id),
                    AuditRow.event == "support_review_completed",
                )
            )
        ).all()
        if not any(
            json.loads(e.details_json).get("attempt_id") == str(result.attempt_id)
            and json.loads(e.details_json).get("receipt_sha256") == receipt.digest()
            for e in events
        ):
            raise WorkflowError("semantic_support_review_audit_missing")

    async def _evidence(
        self,
        session,
        claim: CandidateClaim,
        scope_ids=None,
    ) -> tuple[EvidenceInput, ...]:
        saved = await session.get(ClaimRow, str(claim.claim_id))
        if saved and (
            saved.run_id != str(claim.run_id)
            or saved.task_id != str(claim.originating_research_task_id)
            or saved.claim_text != claim.claim_text
            or CandidateClaim.model_validate_json(saved.candidate_json) != claim
        ):
            raise WorkflowError("candidate_claim_mismatch")
        task = await session.get(SubquestionRow, str(claim.originating_research_task_id))
        if task is None or task.run_id != str(claim.run_id):
            raise WorkflowError("claim_task_mismatch")
        allowed = set(json.loads(task.passage_ids_json))
        source_ids = set(json.loads(task.source_ids_json))
        root_id = task.parent_task_id or task.task_id
        family = (
            await session.scalars(
                select(SubquestionRow).where(
                    SubquestionRow.run_id == str(claim.run_id),
                    (SubquestionRow.task_id == root_id)
                    | (SubquestionRow.parent_task_id == root_id),
                )
            )
        ).all()
        for related in family:
            allowed.update(json.loads(related.passage_ids_json))
            source_ids.update(json.loads(related.source_ids_json))
        items = []
        scope_ids = await self._scope(session, claim) if scope_ids is None else scope_ids
        # Bound reads to this scope and this session. Do not cache across verifier/report checks:
        # each later call must observe and reject changed source versions.
        scope_text = tuple(str(e) for e in scope_ids)
        passages = {
            p.passage_id: p
            for p in (
                await session.scalars(
                    select(PassageRow).where(PassageRow.passage_id.in_(scope_text))
                )
            ).all()
        }
        sources = {
            s.source_id: s
            for s in (
                await session.scalars(
                    select(SourceRow).where(
                        SourceRow.source_id.in_({p.source_id for p in passages.values()})
                    )
                )
            ).all()
        }
        extracted_rows = await session.execute(
            select(ExtractionRow.source_id, ExtractionRow.normalized_url).where(
                ExtractionRow.source_id.in_(sources),
                ExtractionRow.run_id == str(claim.run_id),
                ExtractionRow.status.in_(["success", "cache_hit"]),
            )
        )
        extracted = set(extracted_rows)
        associations = {
            a.passage_id: a
            for a in (
                await session.scalars(
                    select(ClaimEvidenceRow).where(
                        ClaimEvidenceRow.claim_id == str(claim.claim_id),
                        ClaimEvidenceRow.passage_id.in_(scope_text),
                    )
                )
            ).all()
        }
        digests = {}
        for evidence_id in scope_ids:
            passage = passages.get(str(evidence_id))
            if passage is None:
                raise WorkflowError("evidence_id_not_found")
            source = sources.get(passage.source_id)
            if source is None:
                raise WorkflowError("evidence_source_missing")
            if source.run_id != str(claim.run_id):
                raise WorkflowError("evidence_run_mismatch")
            if str(evidence_id) not in allowed or source.source_id not in source_ids:
                raise WorkflowError("evidence_task_mismatch")
            if source.source_id not in digests:
                digests[source.source_id] = sha256(source.content.encode()).hexdigest()
            digest = digests[source.source_id]
            if (
                not source.content.strip()
                or digest != source.content_sha256
                or source.original_chars < len(source.content)
                or not re.fullmatch(r"[0-9a-f]{64}", source.full_content_sha256)
                or (
                    source.original_chars == len(source.content)
                    and source.full_content_sha256 != digest
                )
            ):
                raise WorkflowError("evidence_content_hash_mismatch")
            if (
                passage.end_char > len(source.content)
                or passage.start_char < 0
                or passage.end_char <= passage.start_char
                or not passage.text.strip()
                or source.content[passage.start_char : passage.end_char] != passage.text
            ):
                raise WorkflowError("evidence_passage_mismatch")
            stable = uuid5(
                UUID(source.source_id),
                f"{passage.start_char}:{passage.end_char}:"
                f"{sha256(passage.text.encode()).hexdigest()}",
            )
            if stable != evidence_id:
                raise WorkflowError("evidence_passage_id_mismatch")
            try:
                if normalize_url(source.original_url) != source.normalized_url:
                    raise ValueError("Invalid provenance.")
            except (ValueError, InvalidSourceURL):
                raise WorkflowError("evidence_url_mismatch") from None
            if (source.source_id, source.normalized_url) not in extracted:
                raise WorkflowError("evidence_extraction_missing")
            association = associations.get(str(evidence_id))
            if saved and association is None:
                raise WorkflowError("evidence_version_link_missing")
            if association and association.source_sha256 != digest:
                raise WorkflowError("evidence_version_changed")
            items.append(
                EvidenceInput(
                    evidence_id=evidence_id,
                    source_id=UUID(source.source_id),
                    original_url=source.original_url,
                    source_title=source.title,
                    source_content_sha256=digest,
                    full_content_sha256=source.full_content_sha256,
                    text=passage.text,
                    start_char=passage.start_char,
                    end_char=passage.end_char,
                    locator=passage.locator,
                )
            )
        return tuple(items)

    async def evidence_for(self, claim):
        async with self._sessions() as session:
            return await self._evidence(session, claim)

    async def save_candidates(self, run_id, claims):
        async with self._sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            existing = (
                await session.scalars(select(ClaimRow).where(ClaimRow.run_id == str(run_id)))
            ).all()
            if len(existing) + len(claims) > self._max_claims:
                raise WorkflowError("claim_limit_exceeded")
            for claim in claims:
                if claim.run_id != run_id or await session.get(ClaimRow, str(claim.claim_id)):
                    raise WorkflowError("candidate_claim_collision")
                # Candidate references may be invalid; they remain untrusted in candidate_json.
                # Only actual run/task passages receive FK associations and version hashes.
                task = await session.get(SubquestionRow, str(claim.originating_research_task_id))
                if task is None or task.run_id != str(run_id):
                    raise WorkflowError("claim_task_mismatch")
                session.add(
                    ClaimRow(
                        claim_id=str(claim.claim_id),
                        run_id=str(run_id),
                        task_id=task.task_id,
                        claim_text=claim.claim_text,
                        candidate_json=claim.model_dump_json(),
                        created_at=claim.created_at,
                        verification_status="UNVERIFIED",
                    )
                )
                await session.flush()
                for evidence_id in claim.candidate_evidence_ids:
                    passage = await session.get(PassageRow, str(evidence_id))
                    source = await session.get(SourceRow, passage.source_id) if passage else None
                    if (
                        source
                        and source.run_id == str(run_id)
                        and str(evidence_id) in json.loads(task.passage_ids_json)
                    ):
                        session.add(
                            ClaimEvidenceRow(
                                claim_id=str(claim.claim_id),
                                passage_id=str(evidence_id),
                                source_sha256=source.content_sha256,
                            )
                        )

    async def claims(self, run_id):
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ClaimRow)
                    .where(ClaimRow.run_id == str(run_id))
                    .order_by(ClaimRow.created_at, ClaimRow.claim_id)
                )
            ).all()
            return tuple(CandidateClaim.model_validate_json(row.candidate_json) for row in rows)

    async def begin_attempt(self, claim, *, research_retry_id=None):
        async with self._sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(ClaimRow, str(claim.claim_id))
            if row is None or CandidateClaim.model_validate_json(row.candidate_json) != claim:
                raise WorkflowError("candidate_claim_mismatch")
            attempts = (
                await session.scalars(
                    select(VerificationRow).where(VerificationRow.claim_id == str(claim.claim_id))
                )
            ).all()
            if len(attempts) >= self._max_attempts or any(a.status == "pending" for a in attempts):
                raise WorkflowError("verification_attempt_limit")
            if research_retry_id:
                retry = await session.get(RetryRow, str(research_retry_id))
                task = await session.get(SubquestionRow, row.task_id)
                if (
                    not retry
                    or retry.run_id != str(claim.run_id)
                    or retry.root_task_id != (task.parent_task_id or task.task_id)
                ):
                    raise WorkflowError("verification_retry_scope_mismatch")
            attempt_id = uuid4()
            session.add(
                VerificationRow(
                    attempt_id=str(attempt_id),
                    claim_id=str(claim.claim_id),
                    run_id=str(claim.run_id),
                    sequence=len(attempts) + 1,
                    started_at=utc_now(),
                    status="pending",
                    result_json=None,
                    evidence_scope_json=json.dumps(
                        [str(p) for p in await self._scope(session, claim)]
                    ),
                    research_retry_id=str(research_retry_id) if research_retry_id else None,
                )
            )
            # Re-verification never retains a previously supported status while pending.
            row.verification_status = "UNVERIFIED"
            return attempt_id

    async def finish_attempt(self, result):
        result = VerificationResult.model_validate(result.model_dump())
        started = perf_counter()
        async with self._sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            attempt = await session.get(VerificationRow, str(result.attempt_id))
            if (
                attempt is None
                or attempt.status != "pending"
                or attempt.claim_id != str(result.claim_id)
                or attempt.run_id != str(result.run_id)
            ):
                raise WorkflowError("verification_attempt_mismatch")
            row = await session.get(ClaimRow, attempt.claim_id)
            claim = CandidateClaim.model_validate_json(row.candidate_json)
            scope = tuple(UUID(p) for p in json.loads(attempt.evidence_scope_json))
            if not scope:
                scope = claim.candidate_evidence_ids
            prior = await session.scalar(
                select(VerificationRow)
                .where(
                    VerificationRow.claim_id == attempt.claim_id,
                    VerificationRow.sequence < attempt.sequence,
                )
                .order_by(VerificationRow.sequence.desc())
                .limit(1)
            )
            result = result.model_copy(
                update={
                    "evaluated_evidence_ids": scope if result.llm_requests else (),
                    "research_retry_id": UUID(attempt.research_retry_id)
                    if attempt.research_retry_id
                    else None,
                    "previous_verdict": VerificationResult.model_validate_json(
                        prior.result_json
                    ).verdict.value
                    if prior and prior.result_json
                    else None,
                }
            )
            if result.outcome == "completed":
                try:
                    current = {
                        p.evidence_id: p for p in await self._evidence(session, claim, scope)
                    }
                    self._check_result(claim, result, current)
                    await self._check_support_receipt(session, claim, result)
                except WorkflowError as error:
                    result = result.model_copy(
                        update={
                            "verdict": Verdict.INSUFFICIENT_EVIDENCE,
                            "locked_verdict": "insufficient",
                            "outcome": "validation_rejected",
                            "rejection_reason": error.code,
                            "evidence_ids": (),
                            "exact_supporting_quotes": (),
                            "confidence": None,
                            "explanation": "Evidence failed the transactional integrity gate.",
                        }
                    )
            result = result.model_copy(
                update={"persistence_duration_ms": round((perf_counter() - started) * 1000, 3)}
            )
            attempt.status, attempt.result_json = "finished", result.model_dump_json()
            row.verification_status = result.verdict.value
        return result

    async def collection(self, run_id):
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ClaimRow)
                    .where(ClaimRow.run_id == str(run_id))
                    .order_by(ClaimRow.created_at, ClaimRow.claim_id)
                )
            ).all()
            records = []
            supported = []
            for row in rows:
                attempts = (
                    await session.scalars(
                        select(VerificationRow)
                        .where(VerificationRow.claim_id == row.claim_id)
                        .order_by(VerificationRow.sequence)
                    )
                ).all()
                history = tuple(
                    VerificationAttempt(
                        attempt_id=UUID(a.attempt_id),
                        claim_id=UUID(row.claim_id),
                        started_at=a.started_at,
                        status=a.status,
                        result=VerificationResult.model_validate_json(a.result_json)
                        if a.result_json
                        else None,
                        evidence_scope_ids=tuple(
                            UUID(p) for p in json.loads(a.evidence_scope_json)
                        ),
                        research_retry_id=UUID(a.research_retry_id)
                        if a.research_retry_id
                        else None,
                    )
                    for a in attempts
                )
                candidate = CandidateClaim.model_validate_json(row.candidate_json)
                status = row.verification_status
                integrity_error = None
                # A changed/corrupt persisted source cannot remain in the usable supported set.
                if status == Verdict.SUPPORTED:
                    try:
                        latest = history[-1].result if history else None
                        if (
                            latest is None
                            or latest.outcome != "completed"
                            or latest.verdict != Verdict.SUPPORTED
                        ):
                            raise WorkflowError("verification_history_mismatch")
                        current = {
                            p.evidence_id: p
                            for p in await self._evidence(
                                session,
                                candidate,
                                history[-1].evidence_scope_ids or candidate.candidate_evidence_ids,
                            )
                        }
                        self._check_result(candidate, latest, current)
                        await self._check_support_receipt(session, candidate, latest)
                    except WorkflowError as error:
                        status = "INSUFFICIENT_EVIDENCE"
                        integrity_error = error.code
                    else:
                        supported.append(candidate.claim_id)
                records.append(
                    ClaimRecord(
                        candidate=candidate,
                        verification_status=status,
                        attempts=history,
                        current_integrity_error=integrity_error,
                    )
                )
        return ClaimCollection(
            run_id=run_id, claims=tuple(records), supported_claim_ids=tuple(supported)
        )

    async def attach_retry_evidence(self, run_id, retry_id, claim_ids, evidence_ids):
        async with self._sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            retry = await session.get(RetryRow, str(retry_id))
            if not retry or retry.run_id != str(run_id) or retry.status != "collected":
                raise WorkflowError("invalid_retry_evidence_scope")
            task = await session.get(SubquestionRow, retry.task_id)
            if not {str(p) for p in evidence_ids}.issubset(json.loads(task.passage_ids_json)):
                raise WorkflowError("invalid_retry_evidence_scope")
            for claim_id in claim_ids:
                row = await session.get(ClaimRow, str(claim_id))
                original_task = await session.get(SubquestionRow, row.task_id) if row else None
                if (
                    not row
                    or row.run_id != str(run_id)
                    or (original_task.parent_task_id or original_task.task_id) != retry.root_task_id
                    or row.verification_status not in {"SUPPORTED", "INSUFFICIENT_EVIDENCE"}
                ):
                    raise WorkflowError("invalid_retry_claim_scope")
                claim = CandidateClaim.model_validate_json(row.candidate_json)
                probe = claim.model_copy(
                    update={"claim_id": uuid4(), "candidate_evidence_ids": evidence_ids}
                )
                for evidence in await self._evidence(session, probe, evidence_ids):
                    await session.execute(
                        insert(ClaimEvidenceRow)
                        .values(
                            claim_id=str(claim_id),
                            passage_id=str(evidence.evidence_id),
                            source_sha256=evidence.source_content_sha256,
                        )
                        .on_conflict_do_nothing()
                    )
                row.verification_status = "UNVERIFIED"
