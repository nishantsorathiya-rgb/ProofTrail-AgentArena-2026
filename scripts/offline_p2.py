"""Run the P2 collection use case with labeled provider fixtures; no live requests."""

import asyncio
import json
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx

from app.application.config import Settings
from app.application.runs import ResearchRunService
from app.infrastructure.database import Database, SQLiteRunRepository
from app.infrastructure.evidence_repository import SQLiteEvidenceRepository
from app.retrieval import retrieval_session

ROOT = Path(__file__).resolve().parents[1]


def serialize(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unexpected fixture result type.")


async def run():
    fixtures = ROOT / "tests/fixtures"
    search = json.loads((fixtures / "tavily_search.json").read_text(encoding="utf-8"))
    extract = json.loads((fixtures / "tavily_extract.json").read_text(encoding="utf-8"))

    async def resolver(host, port):
        return ["93.184.216.34"]

    def handler(request):
        assert request.url.host == "api.tavily.com" and request.url.scheme == "https"
        if request.url.path == "/search":
            return httpx.Response(200, json=search)
        if request.url.path != "/extract":
            raise AssertionError("Unexpected provider fixture endpoint.")
        urls = json.loads(request.content)["urls"]
        return httpx.Response(
            200,
            json={
                "results": [item for item in extract["results"] if item["url"] in urls],
                "failed_results": [
                    item for item in extract["failed_results"] if item["url"] in urls
                ],
                "usage": extract["usage"],
            },
        )

    with tempfile.TemporaryDirectory(prefix="prooftrail-p2-offline-") as directory:
        settings = Settings(
            _env_file=None,
            enable_live_requests=False,
            tavily_api_key="offline-fixture-key",
            database_url=f"sqlite:///{(Path(directory) / 'demo.db').as_posix()}",
        )
        database = Database(settings.database_url)
        try:
            await database.initialize()
            run_record = await ResearchRunService(SQLiteRunRepository(database)).create(
                "OFFLINE P2 evidence collection demonstration; no semantic verification.",
            )
            async with retrieval_session(
                settings, mock_transport=httpx.MockTransport(handler), resolver=resolver
            ) as collector:
                initial = await collector.collect(run_record.run_id, "Offline fixture query")
                cached = await collector.collect(
                    run_record.run_id, "Offline repeated fixture query"
                )
            repository = SQLiteEvidenceRepository(database)
            usage = await repository.usage_for_run(run_record.run_id)
            sources = await repository.sources_for_run(run_record.run_id)
            passages = [
                passage
                for source in sources
                for passage in await repository.passages_for_source(source.source_id)
            ]
            assert len(sources) == 1
            assert all(sources[0].content[p.start_char : p.end_char] == p.text for p in passages)
            assert initial.evidence[1].record.status == "failed"
            assert cached.evidence[0].record.status == "cache_hit"
            result = {
                "phase": "P2",
                "mode": "OFFLINE PROVIDER FIXTURES; not live web research",
                "run": asdict(run_record),
                "initial": asdict(initial),
                "cached": asdict(cached),
                "persisted_sources": [asdict(source) for source in sources],
                "persisted_passages": [asdict(passage) for passage in passages],
                "usage": [asdict(attempt) for attempt in usage],
                "exact_passages_valid": True,
                "provider_mock_attempts": len(usage),
                "live_tavily_calls": 0,
                "live_mesh_calls": 0,
                "actual_api_cost": 0,
                "credits_note": "Reported credits in this trace are fixture metadata, not charges.",
            }
        finally:
            await database.aclose()
    output = ROOT / "results/p2_offline_trace.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, default=serialize, indent=2) + "\n", encoding="utf-8")
    print(
        f"Offline P2 trace PASS: {len(sources)} source, {len(passages)} exact passage(s), "
        f"{len(usage)} mock attempts; cache hit and per-URL failure recorded; zero live calls."
    )
    print(f"Saved {output}")


if __name__ == "__main__":
    asyncio.run(run())
