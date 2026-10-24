"""Retrieval records. Extracted text is data, never instructions or verified claims."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4, uuid5

Operation = Literal["search", "extract"]


class RetrievalError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class InvalidSourceURL(RetrievalError):
    pass


class BudgetExceeded(RetrievalError):
    pass


@dataclass(frozen=True, slots=True)
class SearchHit:
    original_url: str
    normalized_url: str
    title: str
    discovery_snippet: str = ""  # Explicitly ineligible for passage persistence.


@dataclass(frozen=True, slots=True)
class SearchResponse:
    hits: tuple[SearchHit, ...]
    rejected_count: int = 0


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    original_url: str
    normalized_url: str
    retrieved_at: datetime
    content: str | None = None
    full_content_sha256: str | None = None
    original_chars: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    source_id: UUID
    run_id: UUID
    original_url: str
    normalized_url: str
    title: str
    retrieved_at: datetime
    content: str
    content_sha256: str  # Exact retained content, used to detect local corruption.
    full_content_sha256: str  # Full extraction response before bounded retention.
    original_chars: int

    def __post_init__(self):
        if (
            not self.content.strip()
            or sha256(self.content.encode()).hexdigest() != self.content_sha256
        ):
            raise RetrievalError("evidence_integrity_failed")
        if self.retrieved_at.tzinfo is None or self.original_chars < len(self.content):
            raise RetrievalError("invalid_source_metadata")

    @property
    def truncated(self) -> bool:
        return self.original_chars > len(self.content)


@dataclass(frozen=True, slots=True)
class EvidencePassage:
    passage_id: UUID
    source_id: UUID
    text: str
    start_char: int
    end_char: int
    locator: str


@dataclass(frozen=True, slots=True)
class ExtractionRecord:
    run_id: UUID
    original_url: str
    normalized_url: str
    attempted_at: datetime
    status: Literal["success", "failed", "cache_hit"]
    source_id: UUID | None = None
    error_code: str | None = None
    record_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class SearchRecord:
    run_id: UUID
    query: str
    retrieved_at: datetime
    result_count: int = 0
    rejected_count: int = 0
    error_code: str | None = None
    record_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class UsageAttempt:
    run_id: UUID
    operation: Operation
    attempt_number: int
    units: int
    mode: Literal["offline_mock", "live"]
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    usage_id: UUID = field(default_factory=uuid4)
    status: Literal["pending", "success", "failed"] = "pending"
    status_code: int | None = None
    error_code: str | None = None
    elapsed_seconds: float = 0
    reported_credits: float | None = None


@dataclass(frozen=True, slots=True)
class SavedEvidence:
    record: ExtractionRecord
    source: SourceSnapshot | None = None
    passages: tuple[EvidencePassage, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionResult:
    search: SearchRecord
    evidence: tuple[SavedEvidence, ...]


def make_snapshot(run_id: UUID, hit: SearchHit, page: ExtractedPage) -> SourceSnapshot:
    if page.error_code or page.content is None or not page.full_content_sha256:
        raise RetrievalError("extraction_has_no_evidence")
    if page.normalized_url != hit.normalized_url:
        raise RetrievalError("source_provenance_mismatch")
    retained_digest = sha256(page.content.encode()).hexdigest()
    source_id = uuid5(
        run_id,
        f"{hit.normalized_url}\n{page.full_content_sha256}\n{retained_digest}\n"
        f"{page.retrieved_at.astimezone(UTC).isoformat()}",
    )
    return SourceSnapshot(
        source_id,
        run_id,
        hit.original_url,
        hit.normalized_url,
        hit.title,
        page.retrieved_at,
        page.content,
        retained_digest,
        page.full_content_sha256,
        page.original_chars,
    )


def passages_for(source: SourceSnapshot) -> tuple[EvidencePassage, ...]:
    # Locators are offsets in Tavily's exact extracted text, not invented DOM/page positions.
    passages = []
    for start in range(0, len(source.content), 2000):
        end = min(len(source.content), start + 2000)
        exact = source.content[start:end]
        if not exact.strip():
            continue
        passages.append(
            EvidencePassage(
                uuid5(source.source_id, f"{start}:{end}:{sha256(exact.encode()).hexdigest()}"),
                source.source_id,
                exact,
                start,
                end,
                f"extracted_text:chars:{start}-{end}",
            )
        )
    return tuple(passages)
