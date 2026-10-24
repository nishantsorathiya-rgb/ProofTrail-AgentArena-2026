"""Owner-only opt-in live Tavily smoke. Never invoked by automated checks."""

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.application.config import Settings
from app.application.runs import ResearchRunService
from app.domain.retrieval import RetrievalError
from app.infrastructure.database import Database, SQLiteRunRepository
from app.infrastructure.evidence_repository import SQLiteEvidenceRepository
from app.retrieval import retrieval_session


async def run(query: str) -> None:
    settings = Settings(
        enable_live_requests=True,
        tavily_max_attempts=1,
        max_searches_per_run=1,
        max_fetches_per_run=1,
    )
    database = Database(settings.database_url)
    try:
        await database.initialize()
        research_run = await ResearchRunService(SQLiteRunRepository(database)).create(query)
        async with retrieval_session(settings, max_candidates=1) as collector:
            result = await collector.collect(research_run.run_id, query)
        usage = await SQLiteEvidenceRepository(database).usage_for_run(research_run.run_id)
        path = Path("results") / f"tavily_live_smoke_{research_run.run_id}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "mode": "LIVE TAVILY; no Mesh calls",
                    "run_id": research_run.run_id,
                    "collection": asdict(result),
                    "usage": [asdict(item) for item in usage],
                    "actual_monetary_cost": None,
                    "cost_note": "Account billing is not available via retrieval.",
                },
                default=serialize,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            f"Live smoke evidence saved to {path}; run remains queued, no research graph executed."
        )
    finally:
        await database.aclose()


def serialize(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unexpected smoke result type.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-live", action="store_true")
    parser.add_argument("--confirmed-free-credits", action="store_true")
    parser.add_argument("--query", required=True)
    args = parser.parse_args()
    if not (args.approved_live and args.confirmed_free_credits):
        parser.error(
            "Owner approval and verified available free credits are required; no requests sent."
        )
    if not args.query.strip() or len(args.query) > 400:
        parser.error("Provide a query from 1 to 400 characters.")
    try:
        asyncio.run(run(args.query))
    except RetrievalError as error:
        print(f"Tavily smoke failed safely: {error.code}; inspect local usage records.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
