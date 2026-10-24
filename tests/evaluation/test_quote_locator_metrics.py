import json
from pathlib import Path

import httpx
import pytest

from app.evaluation.dataset import load_dataset
from app.evaluation.harness import create_harness
from scripts.p6_fixtures import OfflineSemanticFixture


class QuoteOnlyFixture(OfflineSemanticFixture):
    """Explicit v2 mock output, not a production fallback/repair of strict responses."""

    async def mesh(self, request):
        response = await super().mesh(request)
        body = response.json()
        decision = json.loads(body["choices"][0]["message"]["content"])
        decision["contract_version"] = "exact_quotes_v2"
        decision["exact_supporting_quotes"] = [
            {key: q[key] for key in ("evidence_id", "text")}
            for q in decision["exact_supporting_quotes"]
        ]
        body["choices"][0]["message"]["content"] = json.dumps(decision)
        return httpx.Response(200, json=body)


@pytest.mark.parametrize("mode,valid", [("normal", True), ("bad_quote", False)])
async def test_evaluation_scores_resolved_v2_quotes_without_inventing_missing_coordinates(
    tmp_path, mode, valid
):
    harness = await create_harness(tmp_path / "metrics.db")
    try:
        case = load_dataset(Path(__file__).parent / "datasets/claims.v1.json").cases[0]
        provider = QuoteOnlyFixture() if valid else QuoteOnlyFixture(mode)
        row = await harness.evaluate(case, provider)
        assert row["proposed_citations"]
        assert all(c["structurally_valid"] is valid for c in row["proposed_citations"])
        assert row["usable_supported_claim"] is valid
        assert row["reviewed_semantic_correctness"] is None
        if not valid:
            assert row["prediction"]["rejection_reason"] == "verification_quote_missing"
    finally:
        await harness.database.aclose()
