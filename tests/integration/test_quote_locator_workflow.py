"""Exercise the exact production graph and provenance gates using explicit offline providers."""

import pytest

from scripts.offline_quote_locator import run_case


async def test_complete_graph_locates_historical_quotes_then_reviews_and_synthesizes(tmp_path):
    result = await run_case(tmp_path / "positive.db")
    claim = result["claims"]["claims"][0]
    decision = claim["attempts"][0]["result"]
    assert result["fixture_calls"] == {
        "planner": 1, "drafter": 1, "primary": 1, "blind_review": 1,
        "synthesizer": 1, "search": 1, "extract": 4,
    }
    assert decision["verdict"] == "SUPPORTED" and decision["llm_requests"] == 2
    receipt = decision["support_review_receipt"]
    assert receipt["primary_usage_id"] != receipt["review_usage_id"]
    assert len(decision["exact_supporting_quotes"]) == 4
    assert result["report"]["generation_status"] == "model_organized"
    assert result["report"]["completion_status"] == "complete"
    assert len(result["report"]["verified_findings"]) == 1
    assert len(result["report"]["supporting_citations"]) == 4
    events = result["run"]["audit_events"]
    resolved = [e for e in events if e["event"] == "quotation_resolution_completed"]
    assert [e["details"]["role"] for e in resolved] == ["primary", "blind_review"]
    assert all(e["details"]["contract_version"] == "exact_quotes_v2" for e in resolved)
    assert all(e["details"]["quote_count"] == 4 for e in resolved)
    assert resolved[0]["details"]["evidence_sha256"] == receipt["evidence_sha256"]
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert "support_review_completed" in [e["event"] for e in events]
    assert result["final_repository_validation"] == "PASSED"
    assert result["alembic_head"] == "0005" and not result["foreign_key_violations"]
    assert result["live_provider_requests"] == 0


async def test_valid_quotes_cannot_override_blind_semantic_rejection_of_negated_claim(tmp_path):
    result = await run_case(tmp_path / "negative.db", negative=True)
    claim = result["claims"]["claims"][0]
    decision = claim["attempts"][0]["result"]
    assert decision["primary_decision"]["verdict"] == "SUPPORTED"
    assert decision["verdict"] == "CONTRADICTED"
    assert decision["exact_supporting_quotes"]  # All text/locations are structurally valid.
    assert decision["support_review_receipt"]["review_decision"]["verdict"] == "CONTRADICTED"
    assert result["fixture_calls"]["blind_review"] == 1
    assert result["fixture_calls"]["synthesizer"] == 0
    assert result["report"]["verified_findings"] == []
    assert (
        result["report"]["rejected_claims"][0]["candidate_text"] == claim["candidate"]["claim_text"]
    )
    assert result["run"]["retry_history"] == []
    assert result["live_provider_requests"] == 0


@pytest.mark.parametrize("mutation,code", [
    ("wrong_source", "evidence_task_mismatch"),
    ("stale_hash", "evidence_content_hash_mismatch"),
    ("cross_run", "evidence_run_mismatch"),
])
async def test_repository_approval_and_version_checks_precede_v2_locator_and_review(
    tmp_path, mutation, code
):
    result = await run_case(tmp_path / "tamper.db", tamper=mutation)
    record = result["claims"]["claims"][0]
    assert record["attempts"][0]["result"]["rejection_reason"] == code
    assert record["verification_status"] != "SUPPORTED"
    assert result["fixture_calls"]["primary"] == 1
    assert result["fixture_calls"]["blind_review"] == 0
    assert result["report"]["verified_findings"] == []
    assert not any(e["event"] == "quotation_resolution_completed"
                   for e in result["run"]["audit_events"])
    if mutation == "stale_hash":
        assert result["evidence_read_error"] == "evidence_integrity_failed"
