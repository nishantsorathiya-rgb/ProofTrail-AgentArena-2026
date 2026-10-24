import json
from pathlib import Path

import pytest
import pytest_asyncio

from app.agents.verifier import SYSTEM_PROMPT
from app.evaluation.dataset import AdversarialDataset, load_dataset
from app.evaluation.harness import create_harness
from app.evaluation.scoring import summarize
from scripts.p6_fixtures import OfflineSemanticFixture

DATA = Path(__file__).parent / "datasets"
ATTACKS = AdversarialDataset.model_validate_json((DATA / "adversarial.v1.json").read_text())


@pytest_asyncio.fixture
async def harness(tmp_path):
    result = await create_harness(tmp_path / "evaluation.db")
    try:
        yield result
    finally:
        await result.database.aclose()


@pytest.mark.parametrize("case_index", [0, 2, 4])
async def test_real_verifier_persists_three_verdicts_with_exact_quotes_and_audits(
    harness, case_index
):
    case = load_dataset(DATA / "claims.v1.json").cases[case_index]
    row = await harness.evaluate(case, OfflineSemanticFixture())
    assert row["prediction"]["verdict"] == case.expected_verdict.value
    assert row["prediction"]["outcome"] == "completed"
    assert row["persistence_roundtrip_equal"] and row["provider_calls"] == (
        2 if case_index == 0 else 1
    )
    assert row["reviewed_semantic_correctness"] is None
    for citation in row["proposed_citations"]:
        assert citation["structurally_valid"]
    events = [e["event"] for e in row["audit"]]
    assert events[-1] == "verification_attempt_finished"
    assert events.count("verification_attempt_started") == 1
    if case_index == 0:
        assert events[-3:] == [
            "support_review_started",
            "support_review_completed",
            "verification_attempt_finished",
        ]
    assert row["audit"][-1]["details"]["verdict"] == row["prediction"]["verdict"]
    assert row["prediction"]["exact_supporting_quotes"] or case_index == 4


@pytest.mark.parametrize("scenario", ATTACKS.scenarios, ids=lambda s: s.case.challenge)
async def test_adversarial_cases_measure_real_validation_not_just_mock_verdicts(harness, scenario):
    provider = OfflineSemanticFixture(scenario.provider_mode)
    row = await harness.evaluate(scenario.case, provider, mutation=scenario.mutation)
    assert row["persistence_roundtrip_equal"]
    prediction = row["prediction"]
    if scenario.provider_mode == "false_support":
        # The primary semantic error still has valid IDs/quotes, but a separate Mesh review
        # must now reject/withhold it before support or report generation.
        assert prediction["primary_decision"]["verdict"] == "SUPPORTED"
        assert prediction["verdict"] != "SUPPORTED" and not row["usable_supported_claim"]
        assert all(c["structurally_valid"] for c in row["proposed_citations"])
        assert row["provisional_semantic_agreement"] is True
        summary = summarize([row])
        assert summary["semantic_citation_support"]["provisional_false_supported_count"] == 0
        assert summary["reviewed_ground_truth_metrics"]["case_count"] == 0
    elif (
        scenario.provider_mode
        in {
            "fabricated_reference",
            "bad_quote",
            "wrong_offset",
            "wrong_claim",
            "timeout",
            "malformed",
        }
        or scenario.mutation
    ):
        assert prediction["outcome"] != "completed"
        assert prediction["verdict"] == "INSUFFICIENT_EVIDENCE"
        assert not row["usable_supported_claim"]
        assert not prediction["evidence_ids"] and not prediction["exact_supporting_quotes"]
        assert prediction["rejection_reason"]
        if scenario.mutation:
            assert provider.calls == 0  # Integrity/budget gates precede model invocation.
        if scenario.provider_mode in {
            "fabricated_reference",
            "bad_quote",
            "wrong_offset",
            "wrong_claim",
        }:
            assert any(not c["structurally_valid"] for c in row["proposed_citations"])
    else:
        assert row["provisional_semantic_agreement"]
        assert prediction["verdict"] == scenario.case.expected_verdict.value
    if scenario.case.challenge == "prompt_injection":
        assert provider.messages[0]["content"] == SYSTEM_PROMPT
        payload = json.loads(provider.messages[1]["content"])
        assert "IGNORE THE SYSTEM" in payload["UNTRUSTED_EVIDENCE"][0]["text"]
        assert "IGNORE THE SYSTEM" not in provider.messages[0]["content"]
        assert len(provider.messages) == 2
        assert not {"expected_verdict", "explanation", "review_status"} & payload.keys()
