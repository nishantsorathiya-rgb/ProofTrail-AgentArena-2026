from pathlib import Path

import pytest

from app.evaluation.benchmark import BenchmarkDataset, evaluate_assertions
from scripts.p7_benchmark import run_arm

DATASET = BenchmarkDataset.model_validate_json(
    (Path(__file__).parent / "datasets/benchmark.v1.json").read_text()
)


@pytest.mark.parametrize("index", [0, 3])
async def test_paired_real_workflows_share_initial_evidence_and_use_external_proposed_rubric(
    index, tmp_path
):
    case = DATASET.cases[index]
    baseline = await run_arm(case, DATASET, "baseline", tmp_path)
    prooftrail = await run_arm(case, DATASET, "prooftrail", tmp_path)
    assert baseline["initial_evidence"] == prooftrail["initial_evidence"]
    assert baseline["model_configuration"] == prooftrail["model_configuration"]
    assert baseline["question"] == prooftrail["question"] == case.question
    assert baseline["provider_calls"]["verifier"] == baseline["provider_calls"]["followup"] == 0
    assert baseline["api_usage"]["model_requests"] == 2
    assert prooftrail["api_usage"]["model_requests"] > 2
    assert baseline["quality"]["human_reviewed_metrics"] is None
    if case.fault == "negation":
        wrongly_supported = [
            r
            for r in prooftrail["quality"]["failure_examples"]
            if "did not employ" in r["claim_text"]
        ]
        assert not wrongly_supported
        assert any(
            "did not employ" in row["candidate_text"]
            for row in prooftrail["report"]["rejected_claims"]
        )
    else:
        assert prooftrail["quality"]["provisional_synthetic_metrics"]["research_completeness"] == 1


def test_external_judge_never_uses_models_own_supported_flag_as_ground_truth():
    case = DATASET.cases[0]
    assertions = [
        {
            "claim_text": "AlphaCo employed 99 people in 2024.",
            "cited_texts": ["AlphaCo employed 42 people in 2024."],
            "structurally_valid": True,
            "verification": "MODEL_SUPPORTED",
        }
    ]
    judged = evaluate_assertions(assertions, case, DATASET)
    assert judged["provisional_synthetic_metrics"]["unsupported_final_claim_rate"] == 1
    assert judged["provisional_synthetic_metrics"]["semantic_citation_correctness"] == 0
    assert judged["provisional_synthetic_metrics"]["structural_citation_correctness"] == 1
    assert judged["human_reviewed_metrics"] is None


def test_empty_output_never_wins_coverage_by_emitting_nothing():
    scores = evaluate_assertions([], DATASET.cases[0], DATASET)["provisional_synthetic_metrics"]
    assert scores["supported_final_claim_rate"] is None
    assert scores["research_completeness"] == 0
