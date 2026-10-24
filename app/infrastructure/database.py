from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, DateTime, String, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.domain.runs import ResearchRun, RunStatus


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite stores naive UTC; the domain always receives aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("A timezone-aware timestamp is required.")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class ResearchRunRow(Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed','interrupted')",
            name="ck_research_runs_status",
        ),
        CheckConstraint(
            "length(trim(question)) BETWEEN 1 AND 4000",
            name="ck_research_runs_question",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    question: Mapped[str] = mapped_column(String(4000))
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class Database:
    def __init__(self, database_url: str) -> None:
        url = make_url(database_url).set(drivername="sqlite+aiosqlite")
        self._path = Path(url.database) if url.database != ":memory:" else None
        self.engine = create_async_engine(url, echo=False, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def set_pragmas(connection, _record):
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    async def initialize(self) -> None:
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as connection:
            await connection.run_sync(self._migrate)

    @staticmethod
    def _migrate(connection) -> None:
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    async def is_available(self) -> bool:
        try:
            async with self.sessions() as session:
                await session.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    async def aclose(self) -> None:
        await self.engine.dispose()


class SQLiteRunRepository:
    def __init__(self, database: Database) -> None:
        self._sessions = database.sessions

    async def add(self, run: ResearchRun) -> None:
        async with self._sessions.begin() as session:
            session.add(
                ResearchRunRow(
                    run_id=str(run.run_id),
                    question=run.question,
                    status=run.status.value,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                )
            )

    async def get(self, run_id: UUID) -> ResearchRun | None:
        async with self._sessions() as session:
            row = await session.get(ResearchRunRow, str(run_id))
            if row is None:
                return None
            return ResearchRun(
                run_id=UUID(row.run_id),
                question=row.question,
                status=RunStatus(row.status),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
