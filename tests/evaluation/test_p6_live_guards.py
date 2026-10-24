from pathlib import Path

import pytest

from scripts.evaluate_p6_live import reviewed_selection, token_total

DATA = Path(__file__).parent / "datasets/claims.v1.json"


@pytest.mark.parametrize(
    "approved,cap,split,match",
    [
        (False, 1, "development", "approval"),
        (True, 0, "development", "cap"),
        (True, 61, "development", "cap"),
        (True, 1, "held_out", "Held-out"),
        (True, 1, "development", "HUMAN_REVIEWED"),
    ],
)
def test_live_evaluation_refuses_unapproved_unreviewed_or_unbounded_sessions(
    approved, cap, split, match
):
    with pytest.raises(ValueError, match=match):
        reviewed_selection(DATA, None, approved=approved, max_calls=cap, split=split)
    # Unknown provider usage must never be reported as invented zero consumption.
    assert (
        token_total([{"prediction": {"llm_requests": 1, "prompt_tokens": None}}], "prompt_tokens")
        is None
    )
    assert (
        token_total([{"prediction": {"llm_requests": 1, "prompt_tokens": 7}}], "prompt_tokens") == 7
    )
