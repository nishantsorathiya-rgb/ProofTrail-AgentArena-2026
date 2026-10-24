"""Bounded research retry history, frozen verification scopes and grounded final reports."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("subquestions", sa.Column("parent_task_id", sa.String(36)))
    op.add_column(
        "subquestions", sa.Column("retry_number", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "verification_attempts",
        sa.Column("evidence_scope_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column("verification_attempts", sa.Column("research_retry_id", sa.String(36)))
    op.create_table(
        "research_retries",
        sa.Column("retry_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column(
            "root_task_id", sa.String(36), sa.ForeignKey("subquestions.task_id"), nullable=False
        ),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("subquestions.task_id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("root_task_id", "number", name="uq_retry_area_number"),
        sa.UniqueConstraint("task_id"),
        sa.CheckConstraint("number BETWEEN 1 AND 2", name="ck_retry_number"),
    )
    op.create_index("ix_research_retries_run_id", "research_retries", ["run_id"])
    op.create_table(
        "final_reports",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), primary_key=True),
        sa.Column("report_id", sa.String(36), nullable=False, unique=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
    )


def downgrade():
    raise RuntimeError("Restore a reviewed backup instead of deleting retry/report history.")
