import asyncio

from alembic import context

from app.application.config import Settings
from app.infrastructure import (
    claim_models,  # noqa: F401 - register P4 metadata
    evidence_models,  # noqa: F401 - register metadata for Alembic
    report_models,  # noqa: F401 - register P5 metadata
    workflow_models,  # noqa: F401 - register P3 metadata
)
from app.infrastructure.database import Base, Database

config = context.config


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_cli_migrations():
    database = Database(Settings().database_url)
    try:
        if database._path is not None:
            database._path.parent.mkdir(parents=True, exist_ok=True)
        async with database.engine.begin() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await database.aclose()


if context.is_offline_mode():
    raise RuntimeError("Use online SQLite migrations locally; offline SQL export is not supported.")
elif connection := config.attributes.get("connection"):
    run_migrations(connection)
else:
    asyncio.run(run_cli_migrations())
