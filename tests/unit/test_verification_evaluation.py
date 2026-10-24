from pathlib import Path

import pytest

from app.domain.claims import Verdict
from app.evaluation.verification import EvaluationObservation, metrics, reviewed_cases


def test_blank_60_case_template_is_zero_reviewed_ground_truth():
    path = Path(__file__).parents[2] / "Evaluation_Case_Template.csv"
    assert reviewed_cases(path) == ()
    result = metrics(())
    assert result["case_count"] == 0 and result["classification_accuracy"] is None
    assert result["citation_validity"] is None and result["unsupported_claim_rejection"] is None
    assert all(v["precision"] is None and v["recall"] is None for v in result["per_class"].values())


def test_metrics_report_false_support_citation_denominators_and_operational_errors():
    observations = (
        EvaluationObservation(
            case_id="1",
            expected=Verdict.SUPPORTED,
            predicted=Verdict.SUPPORTED,
            emitted_citations=1,
            valid_citations=1,
        ),
        EvaluationObservation(
            case_id="2",
            expected=Verdict.CONTRADICTED,
            predicted=Verdict.SUPPORTED,
            emitted_citations=1,
            valid_citations=0,
        ),
        EvaluationObservation(
            case_id="3",
            expected=Verdict.CONTRADICTED,
            predicted=Verdict.CONTRADICTED,
            emitted_citations=1,
            valid_citations=1,
        ),
        EvaluationObservation(
            case_id="4",
            expected=Verdict.INSUFFICIENT_EVIDENCE,
            predicted=Verdict.INSUFFICIENT_EVIDENCE,
            emitted_citations=0,
            valid_citations=0,
            operational_error=True,
        ),
    )
    result = metrics(observations)
    assert result["classification_accuracy"] == 0.5
    assert result["false_supported_verdicts"] == 1
    assert result["per_class"]["SUPPORTED"]["precision"] == 0.5
    assert result["per_class"]["CONTRADICTED"]["recall"] == 0.5
    assert result["unsupported_claim_rejection"] == 1 / 3
    assert result["citation_validity"] == 2 / 3
    assert result["confusion_matrix"]["INSUFFICIENT_EVIDENCE"]["OPERATIONAL_ERROR"] == 1
    assert result["operational_errors"] == 1
    with pytest.raises(ValueError, match="Duplicate"):
        metrics((observations[0], observations[0]))


def test_approved_but_unpopulated_cases_cannot_be_scored(tmp_path):
    original = (Path(__file__).parents[2] / "Evaluation_Case_Template.csv").read_text()
    path = tmp_path / "bad.csv"
    path.write_text(original.replace("PENDING_REVIEW", "APPROVED", 1))
    with pytest.raises(ValueError, match="complete"):
        reviewed_cases(path)


def test_f1_is_zero_for_observed_classes_with_no_correct_predictions():
    result = metrics(
        tuple(
            EvaluationObservation(
                case_id=str(index),
                expected=expected,
                predicted=predicted,
                emitted_citations=0,
                valid_citations=0,
            )
            for index, (expected, predicted) in enumerate(
                (
                    (Verdict.SUPPORTED, Verdict.CONTRADICTED),
                    (Verdict.CONTRADICTED, Verdict.SUPPORTED),
                )
            )
        )
    )
    assert result["per_class"]["SUPPORTED"]["f1"] == 0
    assert result["per_class"]["CONTRADICTED"]["f1"] == 0
    assert result["per_class"]["INSUFFICIENT_EVIDENCE"]["f1"] is None
