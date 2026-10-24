"""Metrics from actual persisted predictions, with review eligibility kept explicit."""

from app.evaluation.verification import LABELS, EvaluationObservation, metrics


def observations(rows):
    return tuple(
        EvaluationObservation(
            case_id=r["case_id"],
            expected=r["expected_verdict"],
            predicted=r["prediction"]["verdict"],
            emitted_citations=len(r["proposed_citations"]),
            valid_citations=sum(c["structurally_valid"] for c in r["proposed_citations"]),
            operational_error=r["prediction"]["outcome"] == "operational_error",
            validation_rejected=r["prediction"]["outcome"] == "validation_rejected",
        )
        for r in rows
        if r["expected_verdict"] in LABELS
    )


def summarize(rows):
    eligible = [r for r in rows if r["review_status"] == "HUMAN_REVIEWED"]
    provisional = [r for r in rows if r["review_status"] not in {"EXCLUDED", "AMBIGUOUS"}]
    citations = [c for r in rows for c in r["proposed_citations"]]
    fields = (
        "reference_exists",
        "exact_quote_valid",
        "source_hash_consistent",
        "claim_evidence_link_valid",
        "correct_claim_id",
        "structurally_valid",
    )
    approved = [
        r
        for r in provisional
        if r["prediction"]["verdict"] == "SUPPORTED" and r["prediction"]["outcome"] == "completed"
    ]
    reviewed_approved = [r for r in approved if r["review_status"] == "HUMAN_REVIEWED"]
    return {
        "reviewed_ground_truth_metrics": metrics(observations(eligible)),
        "provisional_fixture_agreement_NOT_model_accuracy": metrics(observations(provisional)),
        "structural_citations": {
            "reference_count": len(citations),
            "valid_evidence_references": sum(c["reference_exists"] for c in citations),
            "invalid_evidence_references": sum(not c["reference_exists"] for c in citations),
            "invalid_citation_count": sum(not c["structurally_valid"] for c in citations),
            **{
                f: {
                    "valid": sum(c[f] for c in citations),
                    "total": len(citations),
                    "rate": sum(c[f] for c in citations) / len(citations) if citations else None,
                }
                for f in fields
            },
        },
        "semantic_citation_support": {
            "method": "Claim-label agreement is a proxy only. A correct claim verdict does not "
            "prove each selected quote entails it. Independent citation review is required.",
            "reviewed_supported_count": len(reviewed_approved),
            "reviewed_correct_count": sum(
                r["expected_verdict"] == "SUPPORTED" for r in reviewed_approved
            ),
            "reviewed_citation_semantic_correctness": None,
            "citation_semantic_review_status": "NOT_REVIEWED",
            "provisional_supported_count": len(approved),
            "provisional_false_supported_count": sum(
                r["expected_verdict"] != "SUPPORTED" for r in approved
            ),
        },
        "operational_failures": [
            r["case_id"] for r in rows if r["prediction"]["outcome"] == "operational_error"
        ],
        "validation_rejections": [
            r["case_id"] for r in rows if r["prediction"]["outcome"] == "validation_rejected"
        ],
        "failure_examples": [r for r in rows if not r["provisional_semantic_agreement"]],
    }
