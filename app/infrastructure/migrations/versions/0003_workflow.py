"""Persist P3 workflow snapshots, tasks, ordered audits and model budgets/usage."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_states",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), primary_key=True),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "subquestions",
        sa.Column("task_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("question", sa.String(600), nullable=False),
        sa.Column("search_query", sa.String(400), nullable=False),
        sa.Column("evidence_requirement", sa.String(600), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("source_ids_json", sa.Text(), nullable=False),
        sa.Column("passage_ids_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_subquestions_run_id", "subquestions", ["run_id"])
    op.create_table(
        "run_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("agent", sa.String(16), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(36)),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_table(
        "model_budgets",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_calls", sa.Integer(), nullable=False),
        sa.CheckConstraint("attempts >= 0 AND attempts <= max_calls", name="ck_model_budget"),
    )
    op.create_table(
        "model_usage",
        sa.Column("usage_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("outbound_called", sa.Boolean(), nullable=False),
        sa.Column("elapsed_seconds", sa.Float(), nullable=False),
        sa.Column("model", sa.String(128)),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
    )
    op.create_index("ix_model_usage_run_id", "model_usage", ["run_id"])


def downgrade():
    raise RuntimeError("Restore a reviewed backup instead of deleting workflow audits.")
