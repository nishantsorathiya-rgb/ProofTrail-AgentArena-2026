from pathlib import Path

from app.evaluation.dataset import load_dataset
from scripts.p7_review_cases import review_rows


def test_review_requires_explicit_decisions_and_never_touches_held_out_rows():
    dataset = load_dataset(Path(__file__).parent / "datasets/claims.v1.json")
    rows = [c.model_dump(mode="json") for c in dataset.cases]
    answers = iter(
        [
            "y",
            "Owner checked exact evidence",
            "c",
            "Owner correction test",
            "a",
            "Ambiguous test",
            "x",
            "Invalid synthetic example test",
            "",
            "q",
        ]
    )
    decisions = review_rows(
        rows,
        split="development",
        reviewer="TEST OWNER INPUT",
        ask=lambda _: next(answers),
        show=lambda _: None,
    )
    assert len(decisions) == 4
    assert rows[0]["review_status"] == rows[1]["review_status"] == "HUMAN_REVIEWED"
    assert rows[2]["review_status"] == "AMBIGUOUS" and rows[3]["review_status"] == "EXCLUDED"
    assert rows[4]["review_status"] == "UNREVIEWED"
    assert all(r["review_status"] == "UNREVIEWED" for r in rows if r["dataset_split"] == "held_out")
    assert all(c.review_status == "UNREVIEWED" for c in dataset.cases)


def test_skip_and_correction_without_reason_do_not_approve_rows():
    row = (
        load_dataset(Path(__file__).parent / "datasets/claims.v1.json")
        .cases[0]
        .model_dump(mode="json")
    )
    answers = iter(["s", ""])
    assert (
        review_rows(
            [row],
            split="development",
            reviewer="TEST",
            ask=lambda _: next(answers),
            show=lambda _: None,
        )
        == []
    )
    assert row["review_status"] == "UNREVIEWED"
