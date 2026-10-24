from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base, UTCDateTime


class WorkflowRow(Base):
    __tablename__ = "workflow_states"
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), primary_key=True)
    state_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SubquestionRow(Base):
    __tablename__ = "subquestions"
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    question: Mapped[str] = mapped_column(String(600))
    search_query: Mapped[str] = mapped_column(String(400))
    evidence_requirement: Mapped[str] = mapped_column(String(600))
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration_seconds: Mapped[float] = mapped_column(Float)
    error_code: Mapped[str | None] = mapped_column(String(64))
    source_ids_json: Mapped[str] = mapped_column(Text)
    passage_ids_json: Mapped[str] = mapped_column(Text)
    parent_task_id: Mapped[str | None] = mapped_column(String(36))
    retry_number: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class AuditRow(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime())
    agent: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(36))
    details_json: Mapped[str] = mapped_column(Text)


class ModelBudgetRow(Base):
    __tablename__ = "model_budgets"
    __table_args__ = (
        CheckConstraint("attempts >= 0 AND attempts <= max_calls", name="ck_model_budget"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer)
    max_calls: Mapped[int] = mapped_column(Integer)


class ModelUsageRow(Base):
    __tablename__ = "model_usage"
    usage_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    outbound_called: Mapped[bool] = mapped_column(Boolean)
    elapsed_seconds: Mapped[float] = mapped_column(Float)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
