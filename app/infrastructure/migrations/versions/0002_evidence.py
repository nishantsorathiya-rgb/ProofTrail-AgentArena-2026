"""Add source snapshots, exact passages, retrieval outcomes, cache and usage budgets."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sources",
        sa.Column("source_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("full_content_sha256", sa.String(64), nullable=False),
        sa.Column("original_chars", sa.Integer(), nullable=False),
        sa.CheckConstraint("original_chars >= length(content)", name="ck_source_size"),
    )
    op.create_index("ix_sources_run_id", "sources", ["run_id"])
    op.create_index("ix_sources_normalized_url", "sources", ["normalized_url"])
    op.create_table(
        "evidence_passages",
        sa.Column("passage_id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.source_id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        sa.Column("locator", sa.String(128), nullable=False),
        sa.UniqueConstraint("source_id", "start_char", "end_char", name="uq_passage_offsets"),
        sa.CheckConstraint(
            "start_char >= 0 AND end_char > start_char AND length(text) = end_char - start_char",
            name="ck_passage_offsets",
        ),
    )
    op.create_index("ix_evidence_passages_source_id", "evidence_passages", ["source_id"])
    op.create_table(
        "extraction_records",
        sa.Column("record_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.source_id")),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.CheckConstraint(
            "(status = 'failed' AND source_id IS NULL AND error_code IS NOT NULL) OR "
            "(status IN ('success','cache_hit') AND source_id IS NOT NULL AND error_code IS NULL)",
            name="ck_extraction_outcome",
        ),
    )
    op.create_index("ix_extraction_records_run_id", "extraction_records", ["run_id"])
    op.create_table(
        "search_records",
        sa.Column("record_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("query", sa.String(400), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
    )
    op.create_index("ix_search_records_run_id", "search_records", ["run_id"])
    op.create_table(
        "extraction_cache",
        sa.Column("normalized_url", sa.Text(), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.source_id"), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "retrieval_budgets",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), primary_key=True),
        sa.Column("searches", sa.Integer(), nullable=False),
        sa.Column("fetches", sa.Integer(), nullable=False),
        sa.Column("max_searches", sa.Integer(), nullable=False),
        sa.Column("max_fetches", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "searches >= 0 AND fetches >= 0 AND searches <= max_searches AND "
            "fetches <= max_fetches",
            name="ck_retrieval_budget",
        ),
    )
    op.create_table(
        "retrieval_usage",
        sa.Column("usage_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("elapsed_seconds", sa.Float(), nullable=False),
        sa.Column("reported_credits", sa.Float()),
        sa.CheckConstraint(
            "operation IN ('search','extract') AND units > 0 AND attempt_number > 0 "
            "AND status IN ('pending','success','failed') AND mode IN "
            "('offline_mock','live') AND (reported_credits IS NULL OR "
            "reported_credits >= 0)",
            name="ck_retrieval_usage",
        ),
    )
    op.create_index("ix_retrieval_usage_run_id", "retrieval_usage", ["run_id"])


def downgrade():
    raise RuntimeError("Restore a reviewed backup instead of deleting retained evidence.")
