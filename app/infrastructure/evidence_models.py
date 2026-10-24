from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base, UTCDateTime


class SourceRow(Base):
    __tablename__ = "sources"
    __table_args__ = (CheckConstraint("original_chars >= length(content)", name="ck_source_size"),)
    source_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    original_url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text, index=True)
    title: Mapped[str] = mapped_column(String(500))
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime())
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    full_content_sha256: Mapped[str] = mapped_column(String(64))
    original_chars: Mapped[int] = mapped_column(Integer)


class PassageRow(Base):
    __tablename__ = "evidence_passages"
    __table_args__ = (
        UniqueConstraint("source_id", "start_char", "end_char", name="uq_passage_offsets"),
        CheckConstraint(
            "start_char >= 0 AND end_char > start_char AND length(text) = end_char - start_char",
            name="ck_passage_offsets",
        ),
    )
    passage_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.source_id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    locator: Mapped[str] = mapped_column(String(128))


class ExtractionRow(Base):
    __tablename__ = "extraction_records"
    __table_args__ = (
        CheckConstraint(
            "(status = 'failed' AND source_id IS NULL AND error_code IS NOT NULL) OR "
            "(status IN ('success','cache_hit') AND source_id IS NOT NULL AND error_code IS NULL)",
            name="ck_extraction_outcome",
        ),
    )
    record_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"))
    original_url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))


class SearchRow(Base):
    __tablename__ = "search_records"
    record_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    query: Mapped[str] = mapped_column(String(400))
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime())
    result_count: Mapped[int] = mapped_column(Integer)
    rejected_count: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))


class CacheRow(Base):
    __tablename__ = "extraction_cache"
    normalized_url: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.source_id"))
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime())


class BudgetRow(Base):
    __tablename__ = "retrieval_budgets"
    __table_args__ = (
        CheckConstraint(
            "searches >= 0 AND fetches >= 0 AND searches <= max_searches "
            "AND fetches <= max_fetches",
            name="ck_retrieval_budget",
        ),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), primary_key=True)
    searches: Mapped[int] = mapped_column(Integer)
    fetches: Mapped[int] = mapped_column(Integer)
    max_searches: Mapped[int] = mapped_column(Integer)
    max_fetches: Mapped[int] = mapped_column(Integer)


class UsageRow(Base):
    __tablename__ = "retrieval_usage"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('search','extract') AND units > 0 AND attempt_number > 0 AND "
            "status IN ('pending','success','failed') AND mode IN ('offline_mock','live') AND "
            "(reported_credits IS NULL OR reported_credits >= 0)",
            name="ck_retrieval_usage",
        ),
    )
    usage_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    operation: Mapped[str] = mapped_column(String(16))
    attempt_number: Mapped[int] = mapped_column(Integer)
    units: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16))
    status_code: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    elapsed_seconds: Mapped[float] = mapped_column(Float)
    reported_credits: Mapped[float | None] = mapped_column(Float)
