from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base, UTCDateTime


class RetryRow(Base):
    __tablename__ = "research_retries"
    __table_args__ = (
        UniqueConstraint("root_task_id", "number", name="uq_retry_area_number"),
        CheckConstraint("number BETWEEN 1 AND 2", name="ck_retry_number"),
    )
    retry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    root_task_id: Mapped[str] = mapped_column(ForeignKey("subquestions.task_id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("subquestions.task_id"), unique=True)
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    record_json: Mapped[str] = mapped_column(Text)


class ReportRow(Base):
    __tablename__ = "final_reports"
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), unique=True)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    report_json: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
