"""Independent, explicit claim/passage rubric for paired benchmark outputs.

Proposed synthetic annotations remain unreviewed. Model verdicts never supply ground truth.
"""

from typing import Literal

from pydantic import Field

from app.domain.claims import Verdict
from app.domain.workflow import TypedRecord


class AssertionRule(TypedRecord):
    claim_text: str
    required_exact_evidence: str
    present_verdict: Verdict
    absent_verdict: Verdict = Verdict.INSUFFICIENT_EVIDENCE
    invalidating_context: str = "Conditional only; if approved:"


class BenchmarkCase(TypedRecord):
    case_id: str
    question: str
    retry_result: Literal["new", "unresolved", "empty", "duplicate_url"]
    initial_empty: bool = False
    fault: Literal["none", "negation", "conditional"] = "none"
    expected_subtopics: tuple[str, ...] = Field(min_length=1)


class BenchmarkDataset(TypedRecord):
    dataset_version: str
    provenance: str
    review_status: Literal["UNREVIEWED", "HUMAN_REVIEWED"]
    reviewer: str
    rules: tuple[AssertionRule, ...]
    cases: tuple[BenchmarkCase, ...]


def judge(claim_text, cited_texts, rules):
    rule = next((r for r in rules if r.claim_text == claim_text), None)
    if rule is None:
        return None
    valid = any(
        rule.required_exact_evidence in text and rule.invalidating_context not in text
        for text in cited_texts
    )
    return rule.present_verdict.value if valid else rule.absent_verdict.value


def evaluate_assertions(assertions, case, dataset):
    evaluated = []
    for assertion in assertions:
        outcome = judge(assertion["claim_text"], assertion["cited_texts"], dataset.rules)
        evaluated.append(
            {
                **assertion,
                "independent_proposed_label": outcome,
                "semantic_citation_support_provisional": outcome == "SUPPORTED",
            }
        )
    count = len(evaluated)
    supported = sum(r["independent_proposed_label"] == "SUPPORTED" for r in evaluated)
    unsupported = sum(
        r["independent_proposed_label"] in {"CONTRADICTED", "INSUFFICIENT_EVIDENCE", "CONFLICTING"}
        for r in evaluated
    )
    structural = sum(r["structurally_valid"] for r in evaluated)
    covered = [
        topic
        for topic in case.expected_subtopics
        if any(
            r["claim_text"] == topic and r["independent_proposed_label"] == "SUPPORTED"
            for r in evaluated
        )
    ]
    provisional = {
        "assertions": count,
        "supported": supported,
        "unsupported": unsupported,
        "unknown": count - supported - unsupported,
        "supported_final_claim_rate": supported / count if count else None,
        "unsupported_final_claim_rate": unsupported / count if count else None,
        "false_supported_rate": unsupported / count if count else None,
        "false_supported_denominator": "All factual assertions emitted by this arm; baseline "
        "assertions are unverified, not verifier approvals.",
        "structural_citation_correctness": structural / count if count else None,
        "semantic_citation_correctness": sum(
            r["structurally_valid"] and r["independent_proposed_label"] == "SUPPORTED"
            for r in evaluated
        )
        / count
        if count
        else None,
        "research_completeness": len(covered) / len(case.expected_subtopics),
        "covered_subtopics": covered,
        "expected_subtopics": list(case.expected_subtopics),
    }
    reviewed = dataset.review_status == "HUMAN_REVIEWED" and bool(dataset.reviewer.strip())
    return {
        "provisional_synthetic_metrics": provisional,
        "human_reviewed_metrics": provisional if reviewed else None,
        "live_model_metrics": None,
        "evaluated_assertions": evaluated,
        "failure_examples": [
            r for r in evaluated if r["independent_proposed_label"] != "SUPPORTED"
        ],
    }
