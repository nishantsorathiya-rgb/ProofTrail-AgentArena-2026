"""Create ResearchRun persistence.

Revision ID: 0001
Revises: none
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("question", sa.String(4000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed','interrupted')",
            name="ck_research_runs_status",
        ),
        sa.CheckConstraint(
            "length(trim(question)) BETWEEN 1 AND 4000",
            name="ck_research_runs_question",
        ),
    )
    op.create_index("ix_research_runs_status", "research_runs", ["status"])


def downgrade() -> None:
    raise RuntimeError("P1 does not support destructive downgrade; restore a database backup.")
