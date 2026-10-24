"""P2 composition root, available to internal callers and the approved smoke command."""

from contextlib import asynccontextmanager

import httpx

from app.application.collection import EvidenceCollector
from app.application.config import Settings
from app.infrastructure.database import Database, SQLiteRunRepository
from app.infrastructure.evidence_repository import SQLiteEvidenceRepository
from app.infrastructure.tavily_extract import TavilyExtractAdapter
from app.infrastructure.tavily_http import TavilyHTTPClient
from app.infrastructure.tavily_search import TavilySearchAdapter
from app.infrastructure.url_policy import PublicSourceURLPolicy, Resolver


@asynccontextmanager
async def retrieval_session(
    settings: Settings,
    *,
    mock_transport: httpx.MockTransport | None = None,
    resolver: Resolver | None = None,
    max_candidates: int = 5,
):
    database = Database(settings.database_url)
    repository = SQLiteEvidenceRepository(database)
    urls = PublicSourceURLPolicy(timeout_seconds=settings.dns_timeout_seconds, resolver=resolver)
    http = TavilyHTTPClient(settings, repository, mock_transport=mock_transport)
    try:
        await database.initialize()
        yield EvidenceCollector(
            TavilySearchAdapter(http, urls),
            TavilyExtractAdapter(http, urls),
            repository,
            SQLiteRunRepository(database),
            urls,
            ttl_seconds=settings.page_cache_ttl_seconds,
            max_source_chars=settings.max_source_text_chars,
            max_candidates=max_candidates,
        )
    finally:
        await http.aclose()
        await database.aclose()
