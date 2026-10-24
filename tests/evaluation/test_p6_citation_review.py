import csv

import pytest

from app.evaluation.citation_review import export_worksheet, score_owner_citations


def test_semantic_citation_review_requires_independent_owner_judgment(tmp_path):
    predictions = [
        {
            "case_id": "TEST",
            "claim_text": "A conditional statement is certain.",
            "evidence_text": "If conditions hold, the statement might occur.",
            "prediction": {"attempt_id": "test-attempt", "verdict": "SUPPORTED"},
            "proposed_citations": [
                {
                    "evidence_id": "test-evidence",
                    "structurally_valid": True,
                    "proposed_quotes": [{"text": "might occur"}],
                }
            ],
        }
    ]
    path = tmp_path / "citation_review.csv"
    export_worksheet(predictions, path)
    assert score_owner_citations(predictions, path)["semantic_support_precision"] is None
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0].update(
        review_status="HUMAN_REVIEWED",
        reviewer="TEST reviewer action",
        semantic_judgment="DOES_NOT_ESTABLISH",
    )

    def save():
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    save()
    scored = score_owner_citations(predictions, path)
    assert scored["reviewed_supported_citations"] == 1
    assert scored["semantic_support_precision"] == 0  # A real quote can fail entailment.
    with pytest.raises(ValueError, match="owner edits"):
        export_worksheet(predictions, path)
    rows[0]["claim_text"] = "Changed claim"
    save()
    with pytest.raises(ValueError, match="context"):
        score_owner_citations(predictions, path)
