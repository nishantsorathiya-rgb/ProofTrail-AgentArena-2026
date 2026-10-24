from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event

from app.domain.workflow import WorkflowError
from app.evaluation.dataset import EvaluationCase
from app.evaluation.harness import create_harness
from app.infrastructure.claim_models import ClaimEvidenceRow
from scripts.p6_fixtures import OfflineSemanticFixture


async def test_multi_passage_reads_are_bounded_and_later_passage_corruption_is_still_rejected(
    tmp_path,
):
    content = "In 2024, Test Archive recorded 42 records. " + "Synthetic context. " * 550
    case = EvaluationCase(
        case_id="READ-TEST",
        claim_text="In 2024, Test Archive recorded 42 records.",
        evidence_text=content,
        evidence_source="fixture:read-test",
        evidence_provenance="Synthetic",
        expected_verdict="SUPPORTED",
        explanation="Fixture only",
        review_status="UNREVIEWED",
        dataset_split="development",
        evidence_kind="synthetic",
        family_id="test",
    )
    harness = await create_harness(Path(tmp_path) / "batch.db")
    try:
        row = await harness.evaluate(case, OfflineSemanticFixture())
        collection = await harness.claims.collection(UUID(row["prediction"]["run_id"]))
        claim = collection.claims[0].candidate
        queries = []

        def count(*args):
            queries.append(args[2])

        event.listen(harness.database.engine.sync_engine, "before_cursor_execute", count)
        evidence = await harness.claims.evidence_for(claim)
        event.remove(harness.database.engine.sync_engine, "before_cursor_execute", count)
        assert len(evidence) >= 5 and len(queries) <= 10
        async with harness.database.sessions.begin() as session:
            link = await session.get(
                ClaimEvidenceRow, (str(claim.claim_id), str(evidence[-1].evidence_id))
            )
            link.source_sha256 = "0" * 64
        with pytest.raises(WorkflowError, match="evidence_version_changed"):
            await harness.claims.evidence_for(claim)
    finally:
        await harness.database.aclose()
