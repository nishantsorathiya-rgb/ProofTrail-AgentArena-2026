import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID
from weakref import WeakValueDictionary

from app.domain.ports import EvidenceStore, ExtractPort, RunRepository, SearchPort, SourceURLPolicy
from app.domain.retrieval import (
    CollectionResult,
    ExtractedPage,
    ExtractionRecord,
    RetrievalError,
    SavedEvidence,
    SearchHit,
    SearchRecord,
    make_snapshot,
    passages_for,
)


class EvidenceCollector:
    """P2 search/extract use case. No model, agent, verifier or report is invoked."""

    def __init__(
        self,
        search: SearchPort,
        extract: ExtractPort,
        evidence: EvidenceStore,
        runs: RunRepository,
        urls: SourceURLPolicy,
        *,
        ttl_seconds: int = 3600,
        max_source_chars: int = 12000,
        max_candidates: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._search, self._extract, self._evidence = search, extract, evidence
        self._runs, self._urls = runs, urls
        self._ttl, self._max_chars, self._clock = ttl_seconds, max_source_chars, clock
        if not 1 <= max_candidates <= 5:
            raise ValueError("Select from 1 to 5 source candidates.")
        self._max_candidates = max_candidates
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def collect(self, run_id: UUID, query: str) -> CollectionResult:
        if not isinstance(query, str) or not query.strip() or len(query) > 400 or "\x00" in query:
            raise RetrievalError("invalid_search_query")
        if await self._runs.get(run_id) is None:
            raise RetrievalError("research_run_missing")
        try:
            found = await self._search.search(query, run_id=run_id)
        except RetrievalError as error:
            await self._evidence.save_search(
                SearchRecord(
                    run_id,
                    query,
                    self._clock(),
                    error_code=error.code,
                )
            )
            raise
        search_record = SearchRecord(
            run_id, query, self._clock(), len(found.hits), found.rejected_count
        )
        await self._evidence.save_search(search_record)
        selected = {hit.normalized_url: hit for hit in found.hits}
        results = await asyncio.gather(
            *(
                self._collect_one(run_id, hit)
                for hit in list(selected.values())[: self._max_candidates]
            )
        )
        return CollectionResult(search_record, tuple(results))

    async def _collect_one(self, run_id: UUID, hit: SearchHit) -> SavedEvidence:
        lock = self._locks.setdefault(hit.normalized_url, asyncio.Lock())
        async with lock:
            timestamp = self._clock()
            try:
                normalized = await self._urls.validate(hit.original_url)
                if normalized != hit.normalized_url:
                    raise RetrievalError("source_provenance_mismatch")
                cached = await self._evidence.cached_source(
                    normalized,
                    now=timestamp,
                    ttl_seconds=self._ttl,
                )
                if cached is not None:
                    page = ExtractedPage(
                        hit.original_url,
                        normalized,
                        cached.retrieved_at,
                        cached.content[: self._max_chars],
                        cached.full_content_sha256,
                        cached.original_chars,
                    )
                    status = "cache_hit"
                else:
                    pages = await self._extract.extract([hit.original_url], run_id=run_id)
                    if len(pages) != 1:
                        raise RetrievalError("extraction_result_ambiguous")
                    page, status = pages[0], "success"
                if page.error_code:
                    raise RetrievalError(page.error_code)
                source = make_snapshot(run_id, hit, page)
                record = ExtractionRecord(
                    run_id, hit.original_url, normalized, timestamp, status, source.source_id
                )
                return await self._evidence.save_evidence(
                    SavedEvidence(
                        record,
                        source,
                        passages_for(source),
                    )
                )
            except RetrievalError as error:
                record = ExtractionRecord(
                    run_id,
                    hit.original_url,
                    hit.normalized_url,
                    timestamp,
                    "failed",
                    error_code=error.code,
                )
                return await self._evidence.save_evidence(SavedEvidence(record))
