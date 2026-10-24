import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.application.config import Settings
from app.application.runs import ResearchRunService
from app.domain.retrieval import BudgetExceeded, InvalidSourceURL, RetrievalError
from app.infrastructure.database import Database, SQLiteRunRepository
from app.infrastructure.evidence_repository import SQLiteEvidenceRepository
from app.infrastructure.tavily_extract import TavilyExtractAdapter
from app.infrastructure.tavily_http import TavilyHTTPClient, retry_after_seconds
from app.infrastructure.tavily_search import TavilySearchAdapter
from app.infrastructure.url_policy import PublicSourceURLPolicy

FIXTURES = Path(__file__).parents[1] / "fixtures"
SEARCH = json.loads((FIXTURES / "tavily_search.json").read_text(encoding="utf-8"))
EXTRACT = json.loads((FIXTURES / "tavily_extract.json").read_text(encoding="utf-8"))


async def public_resolver(host, port):
    return ["93.184.216.34"]


@pytest_asyncio.fixture
async def storage(settings):
    database = Database(settings.database_url)
    try:
        await database.initialize()
        run = await ResearchRunService(SQLiteRunRepository(database)).create("OFFLINE Tavily test")
        yield database, SQLiteEvidenceRepository(database), run
    finally:
        await database.aclose()


def make_client(settings, repository, handler, **options):
    configured = Settings(
        _env_file=None,
        **{
            **settings.model_dump(),
            "tavily_api_key": "offline-tavily-key",
            **options,
        },
    )
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    return TavilyHTTPClient(
        configured, repository, mock_transport=httpx.MockTransport(handler), sleep=fake_sleep
    ), waits


async def test_successful_search_payload_deduplication_usage_and_no_ai_answer(settings, storage):
    _, repository, run = storage
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.tavily.com/search"
        assert request.headers["authorization"] == "Bearer offline-tavily-key"
        body = json.loads(request.content)
        assert body["search_depth"] == "basic" and body["max_results"] == 5
        assert body["include_answer"] is False and body["auto_parameters"] is False
        assert body["include_usage"] is True
        assert all(value == 12 for value in request.extensions["timeout"].values())
        return httpx.Response(200, json=SEARCH)

    client, _ = make_client(settings, repository, handler)
    try:
        result = await TavilySearchAdapter(
            client,
            PublicSourceURLPolicy(
                resolver=public_resolver,
            ),
        ).search("Offline search", run_id=run.run_id)
        assert len(result.hits) == 2
        assert result.hits[0].original_url == "https://example.com/article"
        assert "DISCOVERY ONLY" in result.hits[0].discovery_snippet
        assert "GENERATED ANSWER" not in str(result)
        usage = await repository.usage_for_run(run.run_id)
        assert len(usage) == len(calls) == 1
        assert usage[0].reported_credits == 1 and usage[0].mode == "offline_mock"
        assert usage[0].status == "success"
    finally:
        await client.aclose()


async def test_empty_search_is_success_with_unknown_usage(settings, storage):
    _, repository, run = storage
    client, _ = make_client(
        settings, repository, lambda request: httpx.Response(200, json={"results": []})
    )
    try:
        result = await TavilySearchAdapter(
            client, PublicSourceURLPolicy(resolver=public_resolver)
        ).search(
            "Empty offline search",
            run_id=run.run_id,
        )
        assert result.hits == ()
        assert (await repository.usage_for_run(run.run_id))[0].reported_credits is None
    finally:
        await client.aclose()


async def test_partial_extract_preserves_original_urls_and_failure(settings, storage):
    _, repository, run = storage
    original = "https://EXAMPLE.com:443/article#section"
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=EXTRACT)

    client, _ = make_client(settings, repository, handler)
    try:
        pages = await TavilyExtractAdapter(
            client, PublicSourceURLPolicy(resolver=public_resolver)
        ).extract(
            [original, "https://example.com/article", "https://example.org/unavailable"],
            run_id=run.run_id,
        )
        assert requests[0]["urls"] == [original, "https://example.org/unavailable"]
        assert requests[0]["format"] == "text" and requests[0]["extract_depth"] == "basic"
        assert requests[0]["timeout"] == 12
        assert len(pages) == 2 and pages[0].original_url == original
        assert pages[0].content == EXTRACT["results"][0]["raw_content"]
        assert pages[1].error_code == "provider_extraction_failed" and pages[1].content is None
        assert "PRIVATE" not in str(pages)
        assert (await repository.usage_for_run(run.run_id))[0].units == 2
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "exception,expected",
    [
        (httpx.ReadTimeout("PRIVATE timeout"), "tavily_timeout"),
        (httpx.ConnectError("PRIVATE connection"), "tavily_connection_failed"),
    ],
)
async def test_timeouts_and_connection_errors_retry_with_backoff(
    settings, storage, exception, expected
):
    _, repository, run = storage
    calls = []

    def handler(request):
        calls.append(request)
        raise exception

    client, waits = make_client(settings, repository, handler)
    try:
        with pytest.raises(RetrievalError, match=expected):
            await TavilySearchAdapter(
                client, PublicSourceURLPolicy(resolver=public_resolver)
            ).search(
                "Timeout fixture",
                run_id=run.run_id,
            )
        assert len(calls) == 3 and waits == [0.5, 1.0]
        usage = await repository.usage_for_run(run.run_id)
        assert [item.attempt_number for item in usage] == [1, 2, 3]
        assert all(item.error_code == expected and item.reported_credits is None for item in usage)
    finally:
        await client.aclose()


@pytest.mark.parametrize("status", [401, 402, 403, 432, 433, 302])
async def test_auth_quota_and_redirect_errors_never_retry_or_fallback(settings, storage, status):
    _, repository, run = storage
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "http://127.0.0.1/"},
            json={"error": "PRIVATE offline-tavily-key"},
        )

    client, waits = make_client(settings, repository, handler)
    try:
        with pytest.raises(RetrievalError, match="tavily_request_failed") as error:
            await TavilySearchAdapter(
                client, PublicSourceURLPolicy(resolver=public_resolver)
            ).search(
                "Error fixture",
                run_id=run.run_id,
            )
        assert len(calls) == 1 and waits == [] and "PRIVATE" not in str(error.value)
        assert str(calls[0].url).startswith("https://api.tavily.com/")
    finally:
        await client.aclose()


async def test_rate_limit_retry_after_and_reported_credit_tracking(settings, storage):
    _, repository, run = storage
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "rate limit"})
            if len(calls) == 1
            else httpx.Response(200, json={"results": [], "usage": {"credits": 1}})
        )

    client, waits = make_client(settings, repository, handler)
    try:
        await TavilySearchAdapter(client, PublicSourceURLPolicy(resolver=public_resolver)).search(
            "429 fixture",
            run_id=run.run_id,
        )
        assert len(calls) == 2 and len(waits) == 1 and 1.9 <= waits[0] <= 2
        attempts = await repository.usage_for_run(run.run_id)
        assert [item.status_code for item in attempts] == [429, 200]
        assert [item.reported_credits for item in attempts] == [None, 1]
    finally:
        await client.aclose()


async def test_long_retry_after_is_not_clamped_to_early_retry(settings, storage):
    _, repository, run = storage
    client, waits = make_client(
        settings,
        repository,
        lambda request: httpx.Response(
            429,
            headers={"Retry-After": "120"},
            json={"error": "rate limit"},
        ),
    )
    try:
        with pytest.raises(RetrievalError, match="tavily_rate_limited"):
            await TavilySearchAdapter(
                client, PublicSourceURLPolicy(resolver=public_resolver)
            ).search(
                "Long Retry-After fixture",
                run_id=run.run_id,
            )
        assert waits == [] and len(await repository.usage_for_run(run.run_id)) == 1
    finally:
        await client.aclose()


async def test_budget_caps_count_retries_and_survive_new_client(settings, storage):
    _, repository, run = storage
    client, _ = make_client(
        settings, repository, lambda request: httpx.Response(503, json={}), max_searches_per_run=1
    )
    policy = PublicSourceURLPolicy(resolver=public_resolver)
    try:
        with pytest.raises(BudgetExceeded):
            await TavilySearchAdapter(client, policy).search("Capped fixture", run_id=run.run_id)
    finally:
        await client.aclose()
    second, _ = make_client(settings, repository, lambda request: pytest.fail("budget reset"))
    try:
        with pytest.raises(BudgetExceeded):
            await TavilySearchAdapter(second, policy).search("New client", run_id=run.run_id)
        assert len(await repository.usage_for_run(run.run_id)) == 1
    finally:
        await second.aclose()


async def test_shared_search_extract_concurrency_is_bounded(settings, storage):
    _, repository, run = storage
    active = peak = 0
    overlapping = asyncio.Event()

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            overlapping.set()
        await asyncio.wait_for(overlapping.wait(), timeout=2)
        active -= 1
        return httpx.Response(
            200, json={"results": []} if request.url.path == "/search" else EXTRACT
        )

    client, _ = make_client(settings, repository, handler, max_parallel_fetches=2)
    policy = PublicSourceURLPolicy(resolver=public_resolver)
    try:
        await asyncio.gather(
            *(
                TavilySearchAdapter(client, policy).search(f"Parallel {i}", run_id=run.run_id)
                for i in range(4)
            ),
            *(
                TavilyExtractAdapter(client, policy).extract(
                    ["https://example.com/article", "https://example.org/unavailable"],
                    run_id=run.run_id,
                )
                for _ in range(2)
            ),
        )
        assert peak == 2
        assert len(await repository.usage_for_run(run.run_id)) == 6
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [{"url": "https://example.net/unknown", "raw_content": "Do not accept"}]},
        {"results": [{"url": "http://127.0.0.1/", "raw_content": "Do not accept"}]},
    ],
)
async def test_extract_provenance_mismatch_is_rejected(settings, storage, payload):
    _, repository, run = storage
    client, _ = make_client(settings, repository, lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(RetrievalError, match="provider_provenance_mismatch"):
            await TavilyExtractAdapter(
                client, PublicSourceURLPolicy(resolver=public_resolver)
            ).extract(
                ["https://example.com/article"],
                run_id=run.run_id,
            )
    finally:
        await client.aclose()


async def test_invalid_source_url_never_reaches_provider(settings, storage):
    _, repository, run = storage
    client, _ = make_client(settings, repository, lambda request: pytest.fail("SSRF escaped"))
    try:
        with pytest.raises(InvalidSourceURL):
            await TavilyExtractAdapter(
                client, PublicSourceURLPolicy(resolver=public_resolver)
            ).extract(
                ["http://169.254.169.254/latest"],
                run_id=run.run_id,
            )
        assert await repository.usage_for_run(run.run_id) == ()
    finally:
        await client.aclose()


async def test_live_default_is_disabled_even_when_credentials_are_present(settings, storage):
    _, repository, run = storage
    configured = Settings(
        _env_file=None, database_url=settings.database_url, tavily_api_key="fake-key"
    )
    client = TavilyHTTPClient(configured, repository)
    try:
        with pytest.raises(RetrievalError, match="live_requests_disabled"):
            await TavilySearchAdapter(
                client, PublicSourceURLPolicy(resolver=public_resolver)
            ).search(
                "Must stay offline",
                run_id=run.run_id,
            )
        assert client._client is None and await repository.usage_for_run(run.run_id) == ()
    finally:
        await client.aclose()


async def test_oversized_response_is_bounded_and_not_retried(settings, storage):
    _, repository, run = storage
    client, _ = make_client(
        settings,
        repository,
        lambda request: httpx.Response(
            200,
            json={"results": [], "padding": "x" * 200},
        ),
        fetch_max_bytes=100,
    )
    try:
        with pytest.raises(RetrievalError, match="tavily_response_too_large"):
            await TavilySearchAdapter(
                client, PublicSourceURLPolicy(resolver=public_resolver)
            ).search(
                "Byte-limit fixture",
                run_id=run.run_id,
            )
        assert len(await repository.usage_for_run(run.run_id)) == 1
    finally:
        await client.aclose()


def test_retry_after_parser_handles_http_date_and_invalid_values():
    assert retry_after_seconds("2") == 2
    assert retry_after_seconds("Tue, 13 Oct 2020 10:00:00 GMT") == 0
    assert retry_after_seconds("invalid") is None
    assert retry_after_seconds("inf") is None


async def test_html_rate_limit_body_still_uses_status_and_retry_after(settings, storage):
    _, repository, run = storage
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="PRIVATE HTML error")
        return httpx.Response(200, json={"results": []})

    client, waits = make_client(settings, repository, handler)
    try:
        await TavilySearchAdapter(client, PublicSourceURLPolicy(resolver=public_resolver)).search(
            "HTML 429 fixture",
            run_id=run.run_id,
        )
        assert len(calls) == 2 and 0.9 <= waits[0] <= 1
        assert (await repository.usage_for_run(run.run_id))[0].error_code == "tavily_rate_limited"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"results": []}, "extraction_result_missing"),
        (
            {"results": [{"url": "https://example.com/article", "raw_content": ""}]},
            "extraction_content_missing",
        ),
        (
            {
                "results": [{"url": "https://example.com/article", "raw_content": "Source text"}],
                "failed_results": [{"url": "https://example.com/article", "error": "failed"}],
            },
            "extraction_result_ambiguous",
        ),
    ],
)
async def test_missing_empty_and_ambiguous_extract_cannot_be_evidence(
    settings, storage, payload, expected
):
    _, repository, run = storage
    client, _ = make_client(settings, repository, lambda request: httpx.Response(200, json=payload))
    try:
        pages = await TavilyExtractAdapter(
            client, PublicSourceURLPolicy(resolver=public_resolver)
        ).extract(
            ["https://example.com/article"],
            run_id=run.run_id,
        )
        assert pages[0].error_code == expected and pages[0].content is None
    finally:
        await client.aclose()


async def test_concurrent_reservations_cannot_exceed_persisted_search_cap(settings, storage):
    _, repository, run = storage
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": []})

    client, _ = make_client(settings, repository, handler, max_searches_per_run=2)
    adapter = TavilySearchAdapter(client, PublicSourceURLPolicy(resolver=public_resolver))
    try:
        results = await asyncio.gather(
            *(adapter.search(f"Budget {i}", run_id=run.run_id) for i in range(4)),
            return_exceptions=True,
        )
        assert sum(isinstance(item, BudgetExceeded) for item in results) == 2
        assert len(calls) == len(await repository.usage_for_run(run.run_id)) == 2
    finally:
        await client.aclose()
