import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text, update

from app.application.collection import EvidenceCollector
from app.application.config import Settings
from app.application.runs import ResearchRunService
from app.domain.retrieval import ExtractionRecord, RetrievalError, SavedEvidence
from app.infrastructure.database import Database, SQLiteRunRepository
from app.infrastructure.evidence_models import CacheRow, ExtractionRow, SearchRow, SourceRow
from app.infrastructure.evidence_repository import SQLiteEvidenceRepository
from app.infrastructure.tavily_extract import TavilyExtractAdapter
from app.infrastructure.tavily_http import TavilyHTTPClient
from app.infrastructure.tavily_search import TavilySearchAdapter
from app.infrastructure.url_policy import PublicSourceURLPolicy
from app.retrieval import retrieval_session

FIXTURES = Path(__file__).parents[1] / "fixtures"
SEARCH = json.loads((FIXTURES / "tavily_search.json").read_text(encoding="utf-8"))
EXTRACT = json.loads((FIXTURES / "tavily_extract.json").read_text(encoding="utf-8"))


async def public_resolver(host, port):
    return ["93.184.216.34"]


@pytest_asyncio.fixture
async def collection(settings):
    database = Database(settings.database_url)
    await database.initialize()
    runs = SQLiteRunRepository(database)
    repository = SQLiteEvidenceRepository(database)
    run = await ResearchRunService(runs).create("OFFLINE collection fixture")
    calls = []
    responses = {"search": SEARCH, "extract": EXTRACT}

    def handler(request):
        body = json.loads(request.content)
        operation = request.url.path.lstrip("/")
        calls.append((operation, body))
        payload = responses[operation]
        if operation == "extract":
            # Provider fixture response is restricted to requested URLs, as in the real contract.
            payload = {
                "results": [row for row in payload["results"] if row["url"] in body["urls"]],
                "failed_results": [
                    row for row in payload.get("failed_results", []) if row["url"] in body["urls"]
                ],
                "usage": payload.get("usage", {}),
            }
        return httpx.Response(200, json=payload)

    client = TavilyHTTPClient(
        Settings(
            _env_file=None,
            **{
                **settings.model_dump(),
                "tavily_api_key": "offline-fixture-key",
            },
        ),
        repository,
        mock_transport=httpx.MockTransport(handler),
    )
    urls = PublicSourceURLPolicy(resolver=public_resolver)
    collector = EvidenceCollector(
        TavilySearchAdapter(client, urls),
        TavilyExtractAdapter(client, urls),
        repository,
        runs,
        urls,
    )
    try:
        yield database, repository, run, collector, calls, responses
    finally:
        await client.aclose()
        await database.aclose()


async def test_collection_persists_exact_evidence_failures_usage_and_untrusted_content(collection):
    database, repository, run, collector, calls, _ = collection
    result = await collector.collect(run.run_id, "Only this owner query may be searched")
    assert len(result.evidence) == 2 and len(calls) == 3
    success, failed = result.evidence
    assert success.record.status == "success" and failed.record.status == "failed"
    assert failed.record.error_code == "provider_extraction_failed"
    assert failed.source is None and failed.passages == ()
    assert success.source.content == EXTRACT["results"][0]["raw_content"]
    assert success.source.content_sha256 == sha256(success.source.content.encode()).hexdigest()
    assert success.source.retrieved_at.tzinfo is UTC
    assert "DISCOVERY ONLY" not in success.source.content
    assert "GENERATED ANSWER" not in success.source.content
    assert "Ignore previous instructions" in success.source.content  # Stored as opaque data.
    assert calls[0][1]["query"] == "Only this owner query may be searched"
    assert [op for op, _ in calls].count("search") == 1
    passages = await repository.passages_for_source(success.source.source_id)
    assert passages == success.passages
    assert all(success.source.content[p.start_char : p.end_char] == p.text for p in passages)
    assert passages[0].locator.startswith("extracted_text:chars:")
    async with database.sessions() as session:
        records = (await session.scalars(select(ExtractionRow))).all()
        assert len(records) == 2
        assert all("PRIVATE" not in (record.error_code or "") for record in records)
    assert len(await repository.usage_for_run(run.run_id)) == 3


async def test_cache_hit_preserves_stable_passage_ids_and_original_retrieval_time(collection):
    _, _, run, collector, calls, responses = collection
    responses["search"] = {"results": SEARCH["results"][:1]}
    first = (await collector.collect(run.run_id, "First query")).evidence[0]
    second = (await collector.collect(run.run_id, "Second query")).evidence[0]
    assert first.record.status == "success" and second.record.status == "cache_hit"
    assert first.source.source_id == second.source.source_id
    assert first.source.retrieved_at == second.source.retrieved_at
    assert first.passages == second.passages
    assert [op for op, _ in calls] == ["search", "extract", "search"]


async def test_expired_cache_refetches_without_mutating_old_evidence(collection):
    database, repository, run, collector, calls, responses = collection
    responses["search"] = {"results": SEARCH["results"][:1]}
    first = (await collector.collect(run.run_id, "First query")).evidence[0]
    async with database.sessions.begin() as session:
        await session.execute(
            update(CacheRow).values(
                retrieved_at=datetime.now(UTC) - timedelta(seconds=3601),
            )
        )
    responses["extract"] = {
        "results": [
            {"url": "https://example.com/article", "raw_content": "Changed exact offline content."}
        ]
    }
    second = (await collector.collect(run.run_id, "Second query")).evidence[0]
    assert second.record.status == "success" and second.source.source_id != first.source.source_id
    assert (await repository.passages_for_source(first.source.source_id)) == first.passages
    assert [op for op, _ in calls].count("extract") == 2


async def test_cache_boundary_and_clock_rollback_are_misses(collection):
    _, repository, run, collector, _, responses = collection
    responses["search"] = {"results": SEARCH["results"][:1]}
    item = (await collector.collect(run.run_id, "Cache boundary")).evidence[0]
    assert (
        await repository.cached_source(
            item.source.normalized_url,
            now=item.source.retrieved_at + timedelta(seconds=3600),
            ttl_seconds=3600,
        )
        is None
    )
    assert (
        await repository.cached_source(
            item.source.normalized_url,
            now=item.source.retrieved_at - timedelta(seconds=1),
            ttl_seconds=3600,
        )
        is None
    )


async def test_cache_can_be_shared_but_source_and_passages_are_scoped_to_new_run(collection):
    database, repository, run, collector, calls, responses = collection
    responses["search"] = {"results": SEARCH["results"][:1]}
    first = (await collector.collect(run.run_id, "First run")).evidence[0]
    second_run = await ResearchRunService(SQLiteRunRepository(database)).create(
        "Second offline run"
    )
    second = (await collector.collect(second_run.run_id, "Second run")).evidence[0]
    assert second.record.status == "cache_hit"
    assert second.source.run_id == second_run.run_id
    assert second.source.source_id != first.source.source_id
    assert second.passages[0].passage_id != first.passages[0].passage_id
    assert second.source.content == first.source.content
    assert len(await repository.sources_for_run(second_run.run_id)) == 1
    assert [op for op, _ in calls].count("extract") == 1


async def test_concurrent_duplicate_requests_extract_once(collection):
    _, _, run, collector, calls, responses = collection
    responses["search"] = {"results": SEARCH["results"][:1]}
    results = await asyncio.gather(
        collector.collect(run.run_id, "Concurrent A"), collector.collect(run.run_id, "Concurrent B")
    )
    assert [op for op, _ in calls].count("extract") == 1
    assert {item.evidence[0].record.status for item in results} == {"success", "cache_hit"}


async def test_corrupted_cached_content_is_never_served(collection):
    database, repository, run, collector, calls, responses = collection
    responses["search"] = {"results": SEARCH["results"][:1]}
    first = (await collector.collect(run.run_id, "Before corruption")).evidence[0]
    async with database.sessions.begin() as session:
        await session.execute(
            update(SourceRow)
            .where(SourceRow.source_id == str(first.source.source_id))
            .values(content="Tampered private content")
        )
    second = (await collector.collect(run.run_id, "After corruption")).evidence[0]
    assert second.record.status == "success" and second.source.content != "Tampered private content"
    assert [op for op, _ in calls].count("extract") == 2
    assert (
        await repository.cached_source(
            second.source.normalized_url, now=datetime.now(UTC), ttl_seconds=3600
        )
        == second.source
    )


async def test_failed_extract_never_persists_search_snippet(collection):
    _, repository, run, collector, _, responses = collection
    responses["extract"] = {"results": [], "failed_results": EXTRACT["failed_results"]}
    result = await collector.collect(run.run_id, "Missing evidence")
    assert all(item.record.status == "failed" and not item.passages for item in result.evidence)
    assert await repository.sources_for_run(run.run_id) == ()


async def test_repository_rejects_forged_passage_and_failed_source_attachment(collection):
    _, repository, run, collector, _, responses = collection
    responses["search"] = {"results": SEARCH["results"][:1]}
    item = (await collector.collect(run.run_id, "Exact evidence")).evidence[0]
    forged = replace(item.passages[0], text="Search snippet cannot be evidence")
    with pytest.raises(RetrievalError, match="passage_not_exact"):
        await repository.save_evidence(replace(item, passages=(forged,)))
    failed = ExtractionRecord(
        run.run_id,
        item.source.original_url,
        item.source.normalized_url,
        datetime.now(UTC),
        "failed",
        error_code="failed",
    )
    with pytest.raises(RetrievalError):
        await repository.save_evidence(SavedEvidence(failed, item.source, item.passages))


async def test_retrieval_session_bounds_retention_and_hashes_full_content(settings):
    database = Database(settings.database_url)
    await database.initialize()
    run = await ResearchRunService(SQLiteRunRepository(database)).create(
        "Size-bound offline fixture"
    )
    await database.aclose()
    full = "Exact Unicode café. " * 500

    def handler(request):
        return (
            httpx.Response(200, json={"results": SEARCH["results"][:1]})
            if request.url.path == "/search"
            else httpx.Response(
                200,
                json={
                    "results": [{"url": "https://example.com/article", "raw_content": full}],
                },
            )
        )

    configured = Settings(
        _env_file=None,
        **{**settings.model_dump(), "tavily_api_key": "fixture-key", "max_source_text_chars": 100},
    )
    async with retrieval_session(
        configured, mock_transport=httpx.MockTransport(handler), resolver=public_resolver
    ) as collector:
        item = (await collector.collect(run.run_id, "Size fixture")).evidence[0]
        assert item.source.content == full[:100] and item.source.truncated
        assert item.source.full_content_sha256 == sha256(full.encode()).hexdigest()
        assert item.source.content_sha256 == sha256(full[:100].encode()).hexdigest()
        assert item.passages[0].end_char == 100


async def test_empty_search_collection_creates_no_sources_or_extract_requests(collection):
    _, repository, run, collector, calls, responses = collection
    responses["search"] = {"results": []}
    result = await collector.collect(run.run_id, "Empty search")
    assert result.search.result_count == 0 and result.evidence == ()
    assert [op for op, _ in calls] == ["search"]
    assert await repository.sources_for_run(run.run_id) == ()


async def test_p1_database_upgrade_retains_existing_run(settings):
    database = Database(settings.database_url)
    try:
        async with database.engine.begin() as connection:

            def migrate_p1(sync):
                config = Config()
                config.set_main_option(
                    "script_location",
                    str(Path(__file__).parents[2] / "app/infrastructure/migrations"),
                )
                config.attributes["connection"] = sync
                command.upgrade(config, "0001")

            await connection.run_sync(migrate_p1)
        run = await ResearchRunService(SQLiteRunRepository(database)).create("Original P1 record")
        await database.initialize()
        assert await SQLiteRunRepository(database).get(run.run_id) == run
        async with database.engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
            )
    finally:
        await database.aclose()


async def test_failed_search_is_persisted_as_failure(collection, monkeypatch):
    database, _, run, collector, _, _ = collection

    async def fail(query, *, run_id):
        raise RetrievalError("tavily_timeout")

    monkeypatch.setattr(collector._search, "search", fail)
    with pytest.raises(RetrievalError, match="tavily_timeout"):
        await collector.collect(run.run_id, "Search timeout")
    async with database.sessions() as session:
        row = await session.scalar(select(SearchRow))
        assert row.error_code == "tavily_timeout" and row.result_count == 0
