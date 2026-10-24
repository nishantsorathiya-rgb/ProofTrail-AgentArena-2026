"""Read configuration without exposing credentials or opening provider connections."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from app.application.config import Settings


def readiness():
    settings = Settings()

    def configured(value, placeholder):
        return bool(
            value
            and value.get_secret_value().strip()
            and value.get_secret_value().strip() != placeholder
        )

    mesh = configured(settings.mesh_api_key, "YOUR_MESH_KEY_HERE")
    tavily = configured(settings.tavily_api_key, "YOUR_TAVILY_KEY_HERE")
    return {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "mode": "LOCAL_READ_ONLY_PREFLIGHT",
        "mesh_credential_configured": mesh,
        "tavily_credential_configured": tavily,
        "configured_model_identifier": settings.mesh_model_id,
        "enable_live_requests": settings.enable_live_requests,
        "live_authentication_verified": False,
        "model_exists_verified": False,
        "structured_response_live_compatibility": "NOT_TESTED",
        "tavily_search_live_compatibility": "NOT_TESTED",
        "tavily_extract_live_compatibility": "NOT_TESTED",
        "persistence_verification_reporting": "OFFLINE_TESTED_ONLY",
        "proposed_smoke_limits": {
            "research_tasks": 1,
            "claims": 1,
            "retries": 0,
            "mesh_completions": 5,
            "search_requests": 1,
            "extract_url_attempts": 1,
            "transport_attempts_per_request": 1,
            "http_timeout_seconds": 12,
            "max_completion_tokens_total": 12288,
        },
        "potential_tavily_credits_formula": "1 basic search + 1/5 basic successful extraction = "
        "1.2 advertised units; reserve up to 2 credits pending rounding/account check",
        "public_tavily_payg_reference_usd_per_credit": 0.008,
        "conservative_tavily_reference_usd": 0.016,
        "pricing_reference": "https://docs.tavily.com/documentation/api-credits",
        "pricing_reference_checked_date": "2026-10-16",
        "mesh_input_price_usd_per_million": None,
        "mesh_output_price_usd_per_million": None,
        "estimated_total_usd": None,
        "owner_maximum_spend_usd": None,
        "ready_for_live_execution": False,
        "blockers": [
            *([] if mesh else ["Missing non-placeholder Mesh key"]),
            *([] if tavily else ["Missing non-placeholder Tavily key"]),
            *([] if settings.mesh_model_id else ["Missing actual Mesh model identifier"]),
            "Mesh model availability and prices/account credits unverified",
            "Owner maximum dollar spend and provider account hard limit unconfirmed",
            "Explicit approval for this capped live session not granted",
            "Live semantic reliability is unvalidated; offline checks are not release approval",
        ],
        "actual_live_requests": 0,
        "actual_api_cost_usd": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = readiness()
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print("Readiness recorded without network calls; live compatibility NOT verified.")


if __name__ == "__main__":
    main()
