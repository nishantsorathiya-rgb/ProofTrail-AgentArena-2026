from pathlib import Path

import pytest
import pytest_asyncio

from app.evaluation.dataset import AdversarialDataset, AdversarialScenario, load_dataset
from app.evaluation.harness import create_harness
from scripts.p7_semantic import trace_to_report

DATA = Path(__file__).parent / "datasets"
SCENARIOS = AdversarialDataset.model_validate_json(
    (DATA / "adversarial.v1.json").read_text()
).scenarios


@pytest_asyncio.fixture
async def harness(tmp_path):
    value = await create_harness(tmp_path / "report-trace.db")
    try:
        yield value
    finally:
        await value.database.aclose()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.case.challenge)
async def test_full_report_path_preserves_all_18_attacks_and_withholds_false_support(
    harness, scenario
):
    result = await trace_to_report(harness, scenario)
    blocked = scenario.case.challenge in {"negation_reversal", "misleading_partial_quote"}
    assert result["release_blocking_defect_reproduced"] is False
    assert result["structural_report_validation_passed"]
    if blocked:
        assert result["semantic_verifier_rejected"] is True
        assert not result["reached_verified_findings"]
        decision = result["observation"]["prediction"]
        assert decision["primary_decision"]["verdict"] == "SUPPORTED"
        assert decision["support_review_receipt"]["review_decision"]["verdict"] != "SUPPORTED"
    elif result["observation"]["prediction"]["outcome"] != "completed":
        assert not result["reached_verified_findings"]
    if scenario.case.expected_verdict.value in {"CONTRADICTED", "CONFLICTING"} and not blocked:
        assert not result["reached_verified_findings"]


async def test_synthesis_attempting_new_facts_is_rejected_through_real_report_builder(harness):
    case = load_dataset(DATA / "claims.v1.json").cases[0]
    result = await trace_to_report(
        harness, AdversarialScenario(case=case, provider_mode="normal"), malicious_synthesis=True
    )
    assert result["synthesis_calls"] == 1
    assert result["synthesis_new_fact_rejected"]
    assert result["report"]["generation_status"] == "deterministic_incomplete"
    assert [f["claim_text"] for f in result["report"]["verified_findings"]] == [case.claim_text]
