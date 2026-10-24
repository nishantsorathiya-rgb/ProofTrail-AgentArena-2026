from pathlib import Path

import pytest

from scripts.p6_workflows import WorkflowDataset, evaluate_workflow

DATASET = WorkflowDataset.model_validate_json(
    (Path(__file__).parent / "datasets/workflows.v1.json").read_text()
)


@pytest.mark.parametrize("case", DATASET.cases, ids=lambda c: c.case_id)
async def test_actual_graph_coverage_retry_and_report_audit_measurements(case, tmp_path):
    row = await evaluate_workflow(case, DATASET, tmp_path)
    assert row["audit_integrity"] and row["provider_counts_match_persistence"]
    assert row["final_findings_subset_of_current_supported"]
    assert row["unsupported_claims_excluded"] and row["report_citation_revalidation_passed"]
    assert row["reviewed_research_coverage"] is None
    assert row["planning_quality_human_score"] is None
    assert row["coverage_denominator"] == 2 and row["human_confirmed_successful_retries"] == 0
    changes = [c for r in row["retry_effectiveness"] for c in r["changes"]]
    if case.retry_result == "new":
        assert row["provisional_fixture_coverage"] == 1
        recovered = [c for c in changes if c["mechanical_recovery_observed"]]
        assert len(recovered) == 1 and recovered[0]["new_cited_evidence_ids"]
        assert recovered[0]["initial_verdict"] == "INSUFFICIENT_EVIDENCE"
        assert recovered[0]["reverification_verdict"] == "SUPPORTED"
        assert row["retry_effectiveness"][0]["additional_search_requests"] == 1
    else:
        assert not any(c["mechanical_recovery_observed"] for c in changes)
        assert len(row["retry_effectiveness"]) == (2 if case.initial_empty else 1)
        assert row["provisional_fixture_coverage"] == (0 if case.initial_empty else 0.5)
