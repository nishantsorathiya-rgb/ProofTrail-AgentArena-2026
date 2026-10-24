"""Offline tests of the single-session observer; no provider client is invoked."""

import httpx
import pytest

from scripts import session_b_live


def request(operation, output=2048):
    if operation == "mesh":
        return httpx.Request("POST", "https://api.meshapi.ai/v1/chat/completions", json={
            "model": "openai/gpt-4.1-mini", "max_tokens": output,
            "messages": [{"role": "user", "content": "OFFLINE guard fixture"}],
        })
    body = (
        {"search_depth": "basic", "include_answer": False, "max_results": 5}
        if operation == "search"
        else {"extract_depth": "basic", "urls": ["https://example.com/source"]}
    )
    return httpx.Request("POST", f"https://api.tavily.com/{operation}", json=body)


@pytest.fixture
def observer(tmp_path, monkeypatch):
    monkeypatch.setattr(session_b_live, "JOURNAL", tmp_path / "offline.json")
    return session_b_live.SessionObserver(["offline-private-sentinel"])


@pytest.mark.parametrize("operation,limit", [("mesh", 8), ("search", 2), ("extract", 10)])
async def test_blocks_outbound_attempt_beyond_approved_request_count(observer, operation, limit):
    for _ in range(limit):
        await observer.before(request(operation))
    with pytest.raises(RuntimeError, match="request_envelope_exhausted"):
        await observer.before(request(operation))
    assert observer.record["counts"][operation] == limit
    assert len(observer.record["provider_requests"]) == limit
    assert observer.record["requested_output_tokens"] == (16384 if operation == "mesh" else 0)
    assert observer.fatal.is_set()


async def test_blocks_output_overflow_without_counting_unsent_request(observer):
    for _ in range(4):
        await observer.before(request("mesh", output=4096))
    with pytest.raises(RuntimeError, match="output_envelope_exhausted"):
        await observer.before(request("mesh", output=4096))
    assert observer.record["counts"]["mesh"] == 4
    assert observer.record["requested_output_tokens"] == 16384


async def test_provider_refusal_prevents_any_subsequent_provider_request(observer):
    original = request("mesh")
    await observer.before(original)
    await observer.after(httpx.Response(429, request=original, json={"error": "offline limit"}))
    with pytest.raises(RuntimeError, match="session_stopped_no_more_provider_requests"):
        await observer.before(request("search"))
    assert observer.record["counts"] == {"mesh": 1, "search": 0, "extract": 0}
    assert observer.record["provider_requests"][0]["status_code"] == 429


def test_journal_redacts_private_values_recursively(observer):
    result = observer.redact({"nested": ["prefix offline-private-sentinel suffix"]})
    assert result == {"nested": ["prefix [REDACTED] suffix"]}
