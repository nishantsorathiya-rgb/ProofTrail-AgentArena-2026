from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.application.runs import ResearchRunService
from app.domain.runs import ResearchRun, RunStatus
from app.infrastructure.database import Database, SQLiteRunRepository


async def test_run_survives_reopen_and_bootstrap_is_idempotent(settings):
    database = Database(settings.database_url)
    await database.initialize()
    service = ResearchRunService(SQLiteRunRepository(database))
    run = await service.create("  Offline persistence test question  ")
    assert run.status is RunStatus.QUEUED
    assert run.question == "Offline persistence test question"
    await database.aclose()

    reopened = Database(settings.database_url)
    try:
        await reopened.initialize()
        await reopened.initialize()
        restored = await ResearchRunService(SQLiteRunRepository(reopened)).get(run.run_id)
        assert restored == run
        assert restored.created_at.tzinfo is UTC
        assert await SQLiteRunRepository(reopened).get(uuid4()) is None
        async with reopened.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0005"
            assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
            assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert await connection.scalar(text("PRAGMA busy_timeout")) == 5000
    finally:
        await reopened.aclose()


async def test_duplicate_id_rolls_back_and_preserves_original(settings):
    database = Database(settings.database_url)
    try:
        await database.initialize()
        repository = SQLiteRunRepository(database)
        original = ResearchRun(question="Original offline record")
        await repository.add(original)
        with pytest.raises(IntegrityError):
            await repository.add(replace(original, question="Duplicate must not replace original"))
        assert await repository.get(original.run_id) == original
        next_run = ResearchRun(question="Connection remains usable after rollback")
        await repository.add(next_run)
        assert await repository.get(next_run.run_id) == next_run
    finally:
        await database.aclose()


async def test_database_rejects_invalid_status_and_utc_round_trip(settings):
    database = Database(settings.database_url)
    try:
        await database.initialize()
        instant = datetime(2026, 10, 13, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        run = ResearchRun(question="UTC fixture", created_at=instant, updated_at=instant)
        repository = SQLiteRunRepository(database)
        await repository.add(run)
        restored = await repository.get(run.run_id)
        assert restored.created_at == instant.astimezone(UTC)
        assert restored.created_at.tzinfo is UTC
        with pytest.raises(IntegrityError):
            async with database.sessions.begin() as session:
                await session.execute(
                    text("UPDATE research_runs SET status='invented' WHERE run_id=:id"),
                    {"id": str(run.run_id)},
                )
        assert (await repository.get(run.run_id)).status is RunStatus.QUEUED
    finally:
        await database.aclose()


async def test_unknown_migration_version_is_rejected(settings):
    database = Database(settings.database_url)
    try:
        await database.initialize()
        async with database.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num='future-unknown'",
                )
            )
        with pytest.raises(Exception, match="future-unknown"):
            await database.initialize()
    finally:
        await database.aclose()


@pytest.mark.parametrize("question", ["", "   ", "x" * 4001])
def test_invalid_questions_never_become_runs(question):
    with pytest.raises(ValueError, match="Question"):
        ResearchRun(question=question)
