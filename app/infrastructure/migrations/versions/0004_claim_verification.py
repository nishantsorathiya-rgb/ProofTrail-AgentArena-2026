"""Immutable candidate claims, versioned evidence links and append-only attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "candidate_claims",
        sa.Column("claim_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("subquestions.task_id"), nullable=False),
        sa.Column("claim_text", sa.String(1500), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.CheckConstraint("length(trim(claim_text)) BETWEEN 1 AND 1500", name="ck_claim_text"),
        sa.CheckConstraint(
            "verification_status IN ('UNVERIFIED','SUPPORTED','CONTRADICTED',"
            "'INSUFFICIENT_EVIDENCE','CONFLICTING')",
            name="ck_claim_status",
        ),
    )
    op.create_index("ix_candidate_claims_run_id", "candidate_claims", ["run_id"])
    op.create_table(
        "claim_evidence",
        sa.Column(
            "claim_id", sa.String(36), sa.ForeignKey("candidate_claims.claim_id"), primary_key=True
        ),
        sa.Column(
            "passage_id",
            sa.String(36),
            sa.ForeignKey("evidence_passages.passage_id"),
            primary_key=True,
        ),
        sa.Column("source_sha256", sa.String(64), nullable=False),
    )
    op.create_table(
        "verification_attempts",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column(
            "claim_id", sa.String(36), sa.ForeignKey("candidate_claims.claim_id"), nullable=False
        ),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.UniqueConstraint("claim_id", "sequence", name="uq_claim_attempt_sequence"),
        sa.CheckConstraint(
            "status IN ('pending','finished','interrupted') AND sequence > 0",
            name="ck_verification_attempt",
        ),
    )
    op.create_index("ix_verification_attempts_claim_id", "verification_attempts", ["claim_id"])
    op.create_index("ix_verification_attempts_run_id", "verification_attempts", ["run_id"])


def downgrade():
    raise RuntimeError("Restore a reviewed backup instead of deleting verification history.")
