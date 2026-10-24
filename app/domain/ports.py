from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.retrieval import (
    ExtractedPage,
    SavedEvidence,
    SearchRecord,
    SearchResponse,
    SourceSnapshot,
    UsageAttempt,
)
from app.domain.runs import ResearchRun


class SearchPort(Protocol):
    async def search(self, query: str, *, run_id: UUID) -> SearchResponse: ...


class ExtractPort(Protocol):
    async def extract(self, urls: list[str], *, run_id: UUID) -> tuple[ExtractedPage, ...]: ...


class SourceURLPolicy(Protocol):
    async def validate(self, url: str) -> str: ...


class UsageStore(Protocol):
    async def reserve_attempt(
        self, attempt: UsageAttempt, *, max_searches: int, max_fetches: int
    ) -> None: ...

    async def finish_attempt(self, attempt: UsageAttempt) -> None: ...


class EvidenceStore(Protocol):
    async def cached_source(
        self, normalized_url: str, *, now: datetime, ttl_seconds: int
    ) -> SourceSnapshot | None: ...

    async def save_evidence(self, evidence: SavedEvidence) -> SavedEvidence: ...

    async def save_search(self, record: SearchRecord) -> None: ...


class RunRepository(Protocol):
    async def add(self, run: ResearchRun) -> None: ...

    async def get(self, run_id: UUID) -> ResearchRun | None: ...


class StorageHealthPort(Protocol):
    async def is_available(self) -> bool: ...
