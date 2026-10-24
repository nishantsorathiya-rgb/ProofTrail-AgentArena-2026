"""Dry-run by default. Live execution requires separate, explicit owner authorization."""

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import perf_counter
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.application.config import Settings
from app.domain.llm import LLMMessage, ModelUnavailable
from app.infrastructure.mesh import MeshLLMAdapter
from app.infrastructure.observability import configure_logging

MODEL = "openai/gpt-4.1-mini"
MODEL_URL = "https://api.meshapi.ai/v1/models/" + MODEL
INPUT_RATE = Decimal("0.40")
OUTPUT_RATE = Decimal("1.60")
INPUT_ALLOWANCE = 512
MAX_OUTPUT = 128
MAX_CAP = Decimal("0.01")
ESTIMATE = (INPUT_RATE * INPUT_ALLOWANCE + OUTPUT_RATE * MAX_OUTPUT) / 1_000_000
MESSAGES = [
    LLMMessage("system", 'Return only a JSON object with exactly this schema: {"status":"ok"}.'),
    LLMMessage("user", "Connection smoke test. Return the requested status object."),
]


class SmokeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["ok"]


class SmokeRejected(Exception):
    """Only static error codes may escape provider parsing."""


def preflight(settings: Settings) -> dict:
    key = settings.mesh_api_key
    return {
        "mode": "LOCAL_PREFLIGHT_NO_API_REQUESTS",
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "preferred_model": MODEL,
        "base_url": "https://api.meshapi.ai/v1",
        "key_configured": bool(key and key.get_secret_value().startswith("rsk_")),
        "model_configuration_matches": settings.mesh_model_id == MODEL,
        "application_live_enabled": settings.enable_live_requests,
        "proposed_http_requests_max": 2,
        "model_metadata_requests_max": 1,
        "completion_requests_max": 1,
        "tavily_requests": 0,
        "transport_retries": 0,
        "completion_output_token_limit": MAX_OUTPUT,
        "fixed_prompt_input_token_allowance_estimated": INPUT_ALLOWANCE,
        "published_input_usd_per_million": str(INPUT_RATE),
        "published_output_usd_per_million": str(OUTPUT_RATE),
        "pricing_source": "https://meshapi.ai/research/value-rankings",
        "maximum_estimated_completion_cost_usd": str(ESTIMATE),
        "proposed_owner_spending_cap_usd": str(MAX_CAP),
        "authorized_spending_usd": "0",
        "live_requests_executed": 0,
        "actual_cost_usd": "0",
        "approval_required": True,
        "limitations": [
            "Model availability and current account prices require approved metadata lookup.",
            "512 input tokens is a conservative allowance, not a server-side tokenizer guarantee.",
            "Provider key spending caps can overshoot by one completion; disable gateway fallback.",
            "Exact billed cost requires separate dashboard/billing evidence, not just token usage.",
            "Tests JSON completion compatibility; semantic accuracy and research E2E are untested.",
        ],
    }


def validate_prices(payload: dict) -> tuple[Decimal, Decimal]:
    try:
        if payload["id"] != MODEL or payload.get("enabled") is False:
            raise SmokeRejected("model_unavailable")
        for capability in (
            "supports_structured_output", "supports_system_prompt", "supports_completions_api",
        ):
            if payload.get(capability) is not True:
                raise SmokeRejected("model_capability_unconfirmed")
        pricing = payload["pricing"]
        if pricing["pricing_unit"] != "per_1m_tokens":
            raise SmokeRejected("pricing_unit_unconfirmed")
        input_rate = Decimal(str(pricing["prompt_usd_per_1m"]))
        output_rate = Decimal(str(pricing["completion_usd_per_1m"]))
        for rate, ceiling in ((input_rate, INPUT_RATE), (output_rate, OUTPUT_RATE)):
            if not rate.is_finite() or rate < 0 or rate > ceiling:
                raise SmokeRejected("price_exceeds_proposal")
        # These are alternate tariffs, not extra fees on this synchronous chat request.
        # The smoke never calls the Batch API. Validate their shape before excluding them.
        batch_fields = {"batch_input_usd_per_1m", "batch_output_usd_per_1m"}
        for name in batch_fields:
            if pricing.get(name) is not None:
                batch_rate = Decimal(str(pricing[name]))
                if not batch_rate.is_finite() or batch_rate < 0:
                    raise SmokeRejected("model_metadata_invalid")
        # Unknown billing dimensions must be reviewed before a completion can be sent.
        known = {
            "pricing_unit", "prompt_usd_per_1m", "completion_usd_per_1m",
            "input_usd_per_unit", "output_usd_per_unit", "cache_read_input_usd_per_1m",
            "effective_date", "discount_pct",
        } | batch_fields
        if any(value is not None for name, value in pricing.items() if name not in known):
            raise SmokeRejected("additional_pricing_dimension_unreviewed")
        return input_rate, output_rate
    except (KeyError, TypeError, InvalidOperation):
        raise SmokeRejected("model_metadata_invalid") from None


async def execute(
    settings: Settings,
    *,
    approved_live: bool,
    max_usd: Decimal,
    confirmed_key_limits: bool,
    mock_transport: httpx.MockTransport | None = None,
) -> dict:
    # Even mocked executions must exercise these gates. No clients exist before this point.
    if not approved_live or not confirmed_key_limits:
        raise SmokeRejected("explicit_approval_and_key_limits_required")
    if not max_usd.is_finite() or not ESTIMATE <= max_usd <= MAX_CAP:
        raise SmokeRejected("spending_cap_rejected")
    key = settings.mesh_api_key
    if not key or not key.get_secret_value().startswith("rsk_") or settings.mesh_model_id != MODEL:
        raise SmokeRejected("private_configuration_required")
    if mock_transport is not None and not isinstance(mock_transport, httpx.MockTransport):
        raise SmokeRejected("only_offline_mock_transport_allowed")
    configure_logging()
    report = {
        "mode": "OFFLINE_MOCK" if mock_transport is not None else "LIVE_OWNER_APPROVED",
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "model_requested": MODEL,
        "approved_cap_usd": str(max_usd),
        "metadata_requests": 0,
        "completion_requests": 0,
        "tavily_requests": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "estimated_usage_cost_usd": None,
        "actual_billed_cost_usd": None,
        "metadata_duration_ms": None,
        "completion_duration_ms": None,
        "passed": False,
        "error_code": None,
        "response_cache_policy": "no-store",
        "semantic_accuracy_measured": False,
        "billing_note": "Usage-rate estimate; confirm the actual charge in Mesh billing privately.",
    }
    total_started = perf_counter()
    adapter = None
    started = total_started
    stage = "metadata"
    try:
        async with httpx.AsyncClient(
            transport=mock_transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=12, follow_redirects=False, trust_env=False,
        ) as client:
            report["metadata_requests"] = 1
            async with client.stream(
                "GET", MODEL_URL,
                headers={"Authorization": "Bearer " + key.get_secret_value()},
            ) as response:
                if response.status_code != 200:
                    raise SmokeRejected("metadata_http_failure")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 65_536:
                        raise SmokeRejected("metadata_too_large")
                input_rate, output_rate = validate_prices(json.loads(content))
        report["metadata_duration_ms"] = round((perf_counter() - started) * 1000, 3)
        report["confirmed_rates_usd_per_million"] = [str(input_rate), str(output_rate)]
        stage = "completion"
        started = perf_counter()
        adapter = MeshLLMAdapter(
            settings.model_copy(update={"enable_live_requests": True, "http_timeout_seconds": 12}),
            mock_transport=mock_transport, disable_response_cache=True,
        )
        report["completion_requests"] = 1
        result = await adapter.complete(MESSAGES, max_tokens=MAX_OUTPUT)
        report["completion_duration_ms"] = round((perf_counter() - started) * 1000, 3)
        # Do not publish arbitrary model-returned strings, payloads, errors or response IDs.
        if not re.fullmatch(r"(?:openai/)?gpt-4\.1-mini(?:-\d{4}-\d{2}-\d{2})?", result.model):
            raise SmokeRejected("unexpected_response_model")
        report["response_model_matches"] = True
        report["prompt_tokens"] = result.prompt_tokens
        report["completion_tokens"] = result.completion_tokens
        if result.prompt_tokens is None or result.completion_tokens is None:
            raise SmokeRejected("token_usage_unavailable")
        report["estimated_usage_cost_usd"] = str(
            (input_rate * result.prompt_tokens + output_rate * result.completion_tokens) / 1_000_000
        )
        if not 0 <= result.prompt_tokens <= INPUT_ALLOWANCE or not (
            0 <= result.completion_tokens <= MAX_OUTPUT
        ):
            raise SmokeRejected("reported_token_budget_exceeded")
        SmokeResponse.model_validate_json(result.text)
        report["structured_json_valid"] = True
        report["passed"] = True
    except SmokeRejected as error:
        report["error_code"] = str(error)
    except ModelUnavailable as error:
        report["error_code"] = error.code
    except httpx.TimeoutException:
        report["error_code"] = "metadata_timeout"
    except httpx.HTTPError:
        report["error_code"] = "metadata_connection_failure"
    except (ValueError, TypeError, AttributeError, ValidationError):
        report["error_code"] = "invalid_metadata_or_structured_response"
    finally:
        duration_key = stage + "_duration_ms"
        if report[duration_key] is None:
            report[duration_key] = round((perf_counter() - started) * 1000, 3)
        if adapter is not None:
            await adapter.aclose()
        report["total_duration_ms"] = round((perf_counter() - total_started) * 1000, 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-live", action="store_true")
    parser.add_argument("--confirmed-key-limits", action="store_true")
    parser.add_argument("--max-usd", type=Decimal)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        settings = Settings()
        if args.approved_live:
            if args.max_usd is None or args.output is None:
                raise SmokeRejected("live_requires_cap_and_new_output_file")
            # Reserve a new result file before networking; never overwrite earlier live evidence.
            with args.output.open("x", encoding="utf-8") as stream:
                report = asyncio.run(execute(
                    settings, approved_live=True, max_usd=args.max_usd,
                    confirmed_key_limits=args.confirmed_key_limits,
                ))
                stream.write(json.dumps(report, indent=2) + "\n")
        else:
            report = preflight(settings)
            if args.output:
                with args.output.open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0 if report.get("passed", not args.approved_live) else 1
    except (SmokeRejected, OSError, ValidationError, InvalidOperation):
        print("Smoke refused. Check approval, key setup, caps and a new output path.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
