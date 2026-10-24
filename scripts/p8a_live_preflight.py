"""Prepare a capped live plan locally. This command has no live execution mode."""

import argparse
import json
from pathlib import Path

from scripts.p7_readiness import readiness


def plan():
    current = readiness()
    return {
        "phase": "P8A",
        "mode": "LOCAL_PREFLIGHT_NO_PROVIDER_CONNECTIONS",
        "credentials": {
            "mesh_configured": current["mesh_credential_configured"],
            "tavily_configured": current["tavily_credential_configured"],
            "configured_model_identifier": current["configured_model_identifier"],
            "live_enabled_in_environment": current["enable_live_requests"],
        },
        "proposed_limits": {
            "model_completions": 8,
            "basic_searches": 2,
            "extract_url_attempts": 2,
            "claims": 1,
            "initial_subquestions": 1,
            "total_research_tasks": 2,
            "research_retries": 1,
            "transport_attempts": 1,
            "concurrency": 1,
            "http_timeout_seconds": 12,
            "workflow_timeout_seconds": 120,
        },
        "expected_calls": {
            "no_retry_supported_path": "up to 5 model + 1 Search + up to 2 Extract attempts",
            "one_retry_reverifies_existing_claim": "up to 8 model + 2 Search + 2 Extract",
            "failure_or_no_evidence": "May stop earlier; never fabricate a successful retry/report",
        },
        "retrieval_public_payg_reference": {
            "url": "https://docs.tavily.com/documentation/api-credits",
            "usd_per_credit": 0.008,
            "advertised_units_formula": "2 basic searches + 2/5 successful basic extractions = 2.4",
            "conservative_reserved_credits_pending_account_rounding": 4,
            "conservative_retrieval_only_usd": 0.032,
        },
        "estimated_maximum_total_usd": None,
        "missing_cost_inputs": [
            "Owner-selected model and approved current input/output prices",
            "Model context/output billing bounds, including reasoning/caching/gateway charges",
            "Provider-side limits and possible single-request spend-cap overshoot",
        ],
        "currently_authorized_spending_cap_usd": 0,
        "proposed_owner_cap_usd": None,
        "ready_for_live_execution": False,
        "owner_approval_required": True,
        "approval_reason": "Owner explicitly prohibits real API calls until separate approval",
        "live_requests": 0,
        "actual_api_cost_usd": 0,
        "retry_live_exercised": False,
        "extraction_allocation": "The initial search can consume both extract attempts; "
        "the global cap does not reserve one extraction for a retry. No retry success is assumed.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(plan(), indent=2) + "\n")
    print("Prepared 8/2/2 request-cap proposal locally; authorized spend $0; no live execution.")


if __name__ == "__main__":
    main()
