from app.domain.claims import Verdict
from app.evaluation.verification import EvaluationObservation, metrics


def observation(case_id, expected, predicted, **extra):
    return EvaluationObservation(
        case_id=case_id,
        expected=expected,
        predicted=predicted,
        emitted_citations=0,
        valid_citations=0,
        **extra,
    )


def test_operational_and_integrity_failures_never_become_correct_insufficient_predictions():
    result = metrics(
        (
            observation(
                "timeout",
                Verdict.INSUFFICIENT_EVIDENCE,
                Verdict.INSUFFICIENT_EVIDENCE,
                operational_error=True,
            ),
            observation(
                "citation",
                Verdict.INSUFFICIENT_EVIDENCE,
                Verdict.INSUFFICIENT_EVIDENCE,
                validation_rejected=True,
            ),
            observation("semantic", Verdict.INSUFFICIENT_EVIDENCE, Verdict.INSUFFICIENT_EVIDENCE),
        )
    )
    assert result["classification_accuracy"] == result["unsupported_claim_rejection"] == 1 / 3
    matrix = result["confusion_matrix"]["INSUFFICIENT_EVIDENCE"]
    assert matrix["OPERATIONAL_ERROR"] == matrix["VALIDATION_REJECTED"] == 1
    assert result["per_class"]["INSUFFICIENT_EVIDENCE"]["precision"] == 1
    assert result["per_class"]["INSUFFICIENT_EVIDENCE"]["recall"] == 1 / 3


def test_macro_f1_false_supported_and_zero_denominators_have_explicit_meaning():
    result = metrics(
        (
            observation("a", Verdict.SUPPORTED, Verdict.SUPPORTED),
            observation("b", Verdict.CONTRADICTED, Verdict.SUPPORTED),
            observation("c", Verdict.INSUFFICIENT_EVIDENCE, Verdict.CONTRADICTED),
            observation("d", Verdict.CONTRADICTED, Verdict.INSUFFICIENT_EVIDENCE),
        )
    )
    assert result["false_supported_verdicts"] == 1
    assert result["false_supported_rate"] == 1 / 3
    assert result["unsupported_claim_rejection"] == 2 / 3
    assert result["macro_f1"] == (2 / 3) / 3
    assert metrics(())["macro_f1"] is None
    assert metrics(())["false_supported_rate"] is None
