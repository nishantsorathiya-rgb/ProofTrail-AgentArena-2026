"""Shared bounded HTTP transport for Tavily retrieval endpoints only."""

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic, perf_counter
from typing import TypeVar
from uuid import UUID

import httpx

from app.application.config import Settings
from app.domain.ports import UsageStore
from app.domain.retrieval import Operation, RetrievalError, UsageAttempt
from app.infrastructure.observability import configure_logging

logger = logging.getLogger("prooftrail")
T = TypeVar("T")


def reported_credits(payload: dict) -> float | None:
    value = payload.get("usage", {})
    value = value.get("credits") if isinstance(value, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 0 else None
    return None


def retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                return None
            delay = (instant - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None


class TavilyHTTPClient:
    def __init__(
        self,
        settings: Settings,
        usage: UsageStore,
        *,
        mock_transport: httpx.MockTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        configure_logging()
        if mock_transport is not None and not isinstance(mock_transport, httpx.MockTransport):
            raise TypeError("Only MockTransport may bypass the offline default.")
        self.settings = settings
        self._usage = usage
        self._mock = mock_transport
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(settings.max_parallel_fetches)
        self._client: httpx.AsyncClient | None = None
        self._blocked_until = 0.0
        self._closed = False

    def _get_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise RetrievalError("tavily_client_closed")
        if self._mock is None and not self.settings.enable_live_requests:
            raise RetrievalError("live_requests_disabled")
        key = self.settings.tavily_api_key
        if (
            key is None
            or not key.get_secret_value().strip()
            or key.get_secret_value() == "YOUR_TAVILY_KEY_HERE"
        ):
            raise RetrievalError("tavily_configuration_missing")
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._mock or httpx.AsyncHTTPTransport(retries=0),
                timeout=self.settings.http_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                headers={"Authorization": "Bearer " + key.get_secret_value()},
            )
        return self._client

    async def post(
        self,
        operation: Operation,
        payload: dict,
        *,
        run_id: UUID,
        units: int,
        parse: Callable[[dict], Awaitable[T]],
    ) -> T:
        if operation not in {"search", "extract"}:
            raise RetrievalError("tavily_route_rejected")
        client = self._get_client()
        for number in range(1, self.settings.tavily_max_attempts + 1):
            retryable = False
            retry_delay = None
            async with self._semaphore:
                cooldown = max(0, self._blocked_until - monotonic())
                if cooldown:
                    await self._sleep(cooldown)
                attempt = UsageAttempt(
                    run_id, operation, number, units, "offline_mock" if self._mock else "live"
                )
                await self._usage.reserve_attempt(
                    attempt,
                    max_searches=self.settings.max_searches_per_run,
                    max_fetches=self.settings.max_fetches_per_run,
                )
                started = perf_counter()
                code = None
                status_code = None
                credits = None
                try:
                    # No candidate URL can become the request destination.
                    async with client.stream(
                        "POST", f"https://api.tavily.com/{operation}", json=payload
                    ) as response:
                        status_code = response.status_code
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            body.extend(chunk)
                            if len(body) > self.settings.fetch_max_bytes:
                                raise RetrievalError("tavily_response_too_large")
                        if status_code != 200:
                            try:
                                error_data = json.loads(body)
                                if isinstance(error_data, dict):
                                    credits = reported_credits(error_data)
                            except ValueError:
                                pass
                            retryable = status_code == 429 or status_code in {
                                408,
                                500,
                                502,
                                503,
                                504,
                            }
                            retry_delay = retry_after_seconds(response.headers.get("retry-after"))
                            raise RetrievalError(
                                "tavily_rate_limited"
                                if status_code == 429
                                else "tavily_request_failed"
                            )
                        if "application/json" not in response.headers.get("content-type", ""):
                            raise RetrievalError("tavily_invalid_response")
                        data = json.loads(body)
                        if not isinstance(data, dict):
                            raise RetrievalError("tavily_invalid_response")
                        credits = reported_credits(data)
                        result = await parse(data)
                except httpx.TimeoutException:
                    code, retryable = "tavily_timeout", True
                except httpx.RequestError:
                    code, retryable = "tavily_connection_failed", True
                except RetrievalError as error:
                    code = error.code
                except (ValueError, TypeError, KeyError):
                    code = "tavily_invalid_response"
                except asyncio.CancelledError:
                    code = "tavily_cancelled"
                    raise
                finally:
                    completed = replace(
                        attempt,
                        status="failed" if code else "success",
                        status_code=status_code,
                        error_code=code,
                        reported_credits=credits,
                        elapsed_seconds=round(perf_counter() - started, 6),
                    )
                    await self._usage.finish_attempt(completed)
                    logger.info(
                        "tavily_request",
                        extra={
                            "operation": operation,
                            "attempt_number": number,
                            "status_code": status_code,
                            "error_code": code,
                            "reported_credits": credits,
                            "duration_ms": round(completed.elapsed_seconds * 1000, 3),
                        },
                    )
            if code is None:
                return result
            if not retryable or number == self.settings.tavily_max_attempts:
                raise RetrievalError(code) from None
            if retry_delay is not None and retry_delay > self.settings.tavily_backoff_max_seconds:
                raise RetrievalError(code) from None  # Never retry sooner than provider permits.
            delay = (
                retry_delay
                if retry_delay is not None
                else min(
                    self.settings.tavily_backoff_max_seconds,
                    self.settings.tavily_backoff_initial_seconds * 2 ** (number - 1),
                )
            )
            if status_code == 429:
                self._blocked_until = max(self._blocked_until, monotonic() + delay)
            else:
                await self._sleep(delay)
        raise RetrievalError("tavily_attempts_exhausted")

    async def aclose(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
