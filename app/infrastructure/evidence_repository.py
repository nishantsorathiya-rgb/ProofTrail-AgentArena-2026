from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from app.domain.retrieval import (
    BudgetExceeded,
    EvidencePassage,
    RetrievalError,
    SavedEvidence,
    SearchRecord,
    SourceSnapshot,
    UsageAttempt,
)
from app.infrastructure.database import Database
from app.infrastructure.evidence_models import (
    BudgetRow,
    CacheRow,
    ExtractionRow,
    PassageRow,
    SearchRow,
    SourceRow,
    UsageRow,
)


def row_values(record) -> dict:
    return {
        key: str(value) if isinstance(value, UUID) else value
        for key, value in asdict(record).items()
    }


def snapshot_from(row: SourceRow) -> SourceSnapshot:
    return SourceSnapshot(
        UUID(row.source_id),
        UUID(row.run_id),
        row.original_url,
        row.normalized_url,
        row.title,
        row.retrieved_at,
        row.content,
        row.content_sha256,
        row.full_content_sha256,
        row.original_chars,
    )


class SQLiteEvidenceRepository:
    def __init__(self, database: Database):
        self._sessions = database.sessions

    async def save_search(self, record: SearchRecord) -> None:
        async with self._sessions.begin() as session:
            session.add(SearchRow(**row_values(record)))

    async def cached_source(
        self, normalized_url: str, *, now: datetime, ttl_seconds: int
    ) -> SourceSnapshot | None:
        async with self._sessions() as session:
            cached = await session.get(CacheRow, normalized_url)
            if cached is None or not 0 <= (now - cached.retrieved_at).total_seconds() < ttl_seconds:
                return None
            row = await session.get(SourceRow, cached.source_id)
            if (
                row is None
                or row.normalized_url != normalized_url
                or row.retrieved_at != cached.retrieved_at
            ):
                return None
            try:
                return snapshot_from(row)
            except RetrievalError:
                # Never serve locally corrupted content; a fresh extraction may repair the cache.
                return None

    async def save_evidence(self, evidence: SavedEvidence) -> SavedEvidence:
        source = evidence.source
        record = evidence.record
        if source:
            if source.run_id != record.run_id or source.source_id != record.source_id:
                raise RetrievalError("source_run_mismatch")
            if not evidence.passages:
                raise RetrievalError("missing_exact_passage")
            for passage in evidence.passages:
                if (
                    passage.source_id != source.source_id
                    or source.content[passage.start_char : passage.end_char] != passage.text
                ):
                    raise RetrievalError("passage_not_exact")
        elif evidence.passages or record.status != "failed" or record.source_id is not None:
            raise RetrievalError("failed_extraction_cannot_have_evidence")
        async with self._sessions.begin() as session:
            if source:
                existing = await session.get(SourceRow, str(source.source_id))
                if existing:
                    if existing.content != source.content or existing.run_id != str(source.run_id):
                        raise RetrievalError("source_version_collision")
                    # Cache hits retain immutable snapshot metadata, including the original title.
                    source = snapshot_from(existing)
                    evidence = SavedEvidence(record, source, evidence.passages)
                await session.execute(
                    insert(SourceRow)
                    .values(**row_values(source))
                    .on_conflict_do_nothing(index_elements=["source_id"])
                )
                for passage in evidence.passages:
                    await session.execute(
                        insert(PassageRow)
                        .values(**row_values(passage))
                        .on_conflict_do_nothing(index_elements=["passage_id"])
                    )
                if record.status == "success":
                    cached = insert(CacheRow).values(
                        normalized_url=source.normalized_url,
                        source_id=str(source.source_id),
                        retrieved_at=source.retrieved_at,
                    )
                    await session.execute(
                        cached.on_conflict_do_update(
                            index_elements=["normalized_url"],
                            set_={
                                "source_id": cached.excluded.source_id,
                                "retrieved_at": cached.excluded.retrieved_at,
                            },
                            where=CacheRow.retrieved_at <= source.retrieved_at,
                        )
                    )
            session.add(ExtractionRow(**row_values(record)))
        return evidence

    async def reserve_attempt(
        self, attempt: UsageAttempt, *, max_searches: int, max_fetches: int
    ) -> None:
        if attempt.operation == "search" and attempt.units != 1:
            raise RetrievalError("invalid_search_usage_units")
        async with self._sessions.begin() as session:
            await session.execute(
                insert(BudgetRow)
                .values(
                    run_id=str(attempt.run_id),
                    searches=0,
                    fetches=0,
                    max_searches=max_searches,
                    max_fetches=max_fetches,
                )
                .on_conflict_do_nothing(index_elements=["run_id"])
            )
            counter = BudgetRow.searches if attempt.operation == "search" else BudgetRow.fetches
            stored_cap = (
                BudgetRow.max_searches if attempt.operation == "search" else BudgetRow.max_fetches
            )
            configured_cap = max_searches if attempt.operation == "search" else max_fetches
            updated = await session.execute(
                update(BudgetRow)
                .where(
                    BudgetRow.run_id == str(attempt.run_id),
                    counter + attempt.units <= stored_cap,
                    counter + attempt.units <= configured_cap,
                )
                .values({counter: counter + attempt.units})
            )
            if updated.rowcount != 1:
                raise BudgetExceeded("retrieval_budget_exhausted")
            session.add(UsageRow(**row_values(attempt)))

    async def finish_attempt(self, attempt: UsageAttempt) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(UsageRow, str(attempt.usage_id))
            if row is None or row.run_id != str(attempt.run_id):
                raise RetrievalError("usage_attempt_missing")
            row.status = attempt.status
            row.status_code = attempt.status_code
            row.error_code = attempt.error_code
            row.elapsed_seconds = attempt.elapsed_seconds
            row.reported_credits = attempt.reported_credits

    async def sources_for_run(self, run_id: UUID) -> tuple[SourceSnapshot, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(SourceRow)
                    .where(
                        SourceRow.run_id == str(run_id),
                    )
                    .order_by(SourceRow.retrieved_at, SourceRow.source_id)
                )
            ).all()
            return tuple(snapshot_from(row) for row in rows)

    async def passages_for_source(self, source_id: UUID) -> tuple[EvidencePassage, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(PassageRow)
                    .where(
                        PassageRow.source_id == str(source_id),
                    )
                    .order_by(PassageRow.start_char)
                )
            ).all()
            return tuple(
                EvidencePassage(
                    UUID(row.passage_id),
                    UUID(row.source_id),
                    row.text,
                    row.start_char,
                    row.end_char,
                    row.locator,
                )
                for row in rows
            )

    async def usage_for_run(self, run_id: UUID) -> tuple[UsageAttempt, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(UsageRow)
                    .where(
                        UsageRow.run_id == str(run_id),
                    )
                    .order_by(UsageRow.started_at, UsageRow.usage_id)
                )
            ).all()
            return tuple(
                UsageAttempt(
                    UUID(row.run_id),
                    row.operation,
                    row.attempt_number,
                    row.units,
                    row.mode,
                    row.started_at,
                    UUID(row.usage_id),
                    row.status,
                    row.status_code,
                    row.error_code,
                    row.elapsed_seconds,
                    row.reported_credits,
                )
                for row in rows
            )
