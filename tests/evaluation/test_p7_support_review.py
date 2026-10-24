import json
from pathlib import Path
from uuid import UUID

import pytest

from app.agents.verifier import SUPPORT_REVIEW_PROMPT
from app.evaluation.dataset import AdversarialDataset, load_dataset
from app.evaluation.harness import create_harness
from app.infrastructure.claim_models import VerificationRow
from app.infrastructure.workflow_models import AuditRow
from scripts.p6_fixtures import OfflineSemanticFixture
from scripts.p7_semantic import trace_drafted_attack

DATA = Path(__file__).parent / "datasets"


@pytest.mark.parametrize("index", [3, 4])
async def test_both_original_failures_are_drafted_reviewed_and_withheld_by_actual_graph(
    index, tmp_path
):
    scenario = AdversarialDataset.model_validate_json(
        (DATA / "adversarial.v1.json").read_text()
    ).scenarios[index]
    result = await trace_drafted_attack(scenario.case, tmp_path)
    assert result["drafting_executed"]
    assert result["provider_calls"]["verifier"] == 2
    assert not result["unsupported_reached_verified_findings"]
    record = result["claims"]["claims"][0]
    assert record["candidate"]["claim_text"] == scenario.case.claim_text
    decision = record["attempts"][0]["result"]
    assert decision["primary_decision"]["verdict"] == "SUPPORTED"
    assert decision["verdict"] == scenario.case.expected_verdict.value
    assert (
        decision["support_review_receipt"]["primary_usage_id"]
        != decision["support_review_receipt"]["review_usage_id"]
    )


@pytest.mark.parametrize("fault", ["timeout", "malformed", "citation"])
async def test_review_failure_never_falls_back_to_primary_supported_verdict(fault, tmp_path):
    harness = await create_harness(tmp_path / "review.db")
    try:
        case = load_dataset(DATA / "claims.v1.json").cases[0]
        provider = OfflineSemanticFixture(review_fault=fault)
        row = await harness.evaluate(case, provider)
        assert row["prediction"]["primary_decision"]["verdict"] == "SUPPORTED"
        assert row["prediction"]["verdict"] == "INSUFFICIENT_EVIDENCE"
        assert not row["usable_supported_claim"] and row["prediction"]["llm_requests"] == 2
        assert provider.messages[0]["content"] == SUPPORT_REVIEW_PROMPT
        payload = json.loads(provider.messages[1]["content"])
        assert set(payload) == {"claim", "UNTRUSTED_EVIDENCE", "output_schema"}
        assert payload["claim"]["verification_status"] == "UNVERIFIED"
        assert "primary_decision" not in payload and "verdict" not in payload["claim"]
    finally:
        await harness.database.aclose()


async def test_exhausted_review_budget_withholds_primary_support_without_extra_request(tmp_path):
    harness = await create_harness(tmp_path / "budget.db")
    try:
        case = load_dataset(DATA / "claims.v1.json").cases[0]
        provider = OfflineSemanticFixture()
        row = await harness.evaluate(case, provider, max_calls=1)
        assert provider.calls == row["prediction"]["llm_requests"] == 1
        assert row["prediction"]["rejection_reason"] == "model_budget_exhausted"
        assert row["prediction"]["verdict"] == "INSUFFICIENT_EVIDENCE"
        assert not row["usable_supported_claim"]
    finally:
        await harness.database.aclose()


@pytest.mark.parametrize("tamper", ["legacy_missing", "single_call", "evidence_hash", "audit"])
async def test_unreviewed_or_forged_semantic_receipts_cannot_remain_usable_support(
    tamper, tmp_path
):
    harness = await create_harness(tmp_path / "receipts.db")
    try:
        case = load_dataset(DATA / "claims.v1.json").cases[0]
        row = await harness.evaluate(case, OfflineSemanticFixture())
        assert row["usable_supported_claim"]
        async with harness.database.sessions.begin() as session:
            attempt = await session.get(VerificationRow, row["prediction"]["attempt_id"])
            raw = json.loads(attempt.result_json)
            if tamper == "legacy_missing":
                raw.pop("support_review_receipt")
            elif tamper == "single_call":
                raw["support_review_receipt"]["review_usage_id"] = raw["support_review_receipt"][
                    "primary_usage_id"
                ]
            elif tamper == "evidence_hash":
                raw["support_review_receipt"]["evidence_sha256"] = "0" * 64
            else:
                from sqlalchemy import delete

                await session.execute(
                    delete(AuditRow).where(
                        AuditRow.run_id == row["prediction"]["run_id"],
                        AuditRow.event == "support_review_completed",
                    )
                )
            attempt.result_json = json.dumps(raw)
        collection = await harness.claims.collection(UUID(row["prediction"]["run_id"]))
        assert not collection.supported_claim_ids
        assert collection.claims[0].verification_status == "INSUFFICIENT_EVIDENCE"
        assert "semantic_support_review" in collection.claims[0].current_integrity_error
        assert collection.claims[0].candidate.claim_text == case.claim_text
    finally:
        await harness.database.aclose()
