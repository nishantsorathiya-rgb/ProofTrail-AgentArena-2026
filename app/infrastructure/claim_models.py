from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base, UTCDateTime


class ClaimRow(Base):
    __tablename__ = "candidate_claims"
    __table_args__ = (
        CheckConstraint("length(trim(claim_text)) BETWEEN 1 AND 1500", name="ck_claim_text"),
        CheckConstraint(
            "verification_status IN ('UNVERIFIED','SUPPORTED','CONTRADICTED',"
            "'INSUFFICIENT_EVIDENCE','CONFLICTING')",
            name="ck_claim_status",
        ),
    )
    claim_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("subquestions.task_id"))
    claim_text: Mapped[str] = mapped_column(String(1500))
    candidate_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    verification_status: Mapped[str] = mapped_column(String(32))


class ClaimEvidenceRow(Base):
    __tablename__ = "claim_evidence"
    claim_id: Mapped[str] = mapped_column(ForeignKey("candidate_claims.claim_id"), primary_key=True)
    passage_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_passages.passage_id"), primary_key=True
    )
    source_sha256: Mapped[str] = mapped_column(String(64))


class VerificationRow(Base):
    __tablename__ = "verification_attempts"
    __table_args__ = (
        UniqueConstraint("claim_id", "sequence", name="uq_claim_attempt_sequence"),
        CheckConstraint(
            "status IN ('pending','finished','interrupted') AND sequence > 0",
            name="ck_verification_attempt",
        ),
    )
    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("candidate_claims.claim_id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16))
    result_json: Mapped[str | None] = mapped_column(Text)
    evidence_scope_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    research_retry_id: Mapped[str | None] = mapped_column(String(36))
