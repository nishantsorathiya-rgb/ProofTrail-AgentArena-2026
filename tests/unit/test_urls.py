import asyncio

import pytest

from app.domain.retrieval import InvalidSourceURL
from app.domain.urls import normalize_url
from app.infrastructure.url_policy import PublicSourceURLPolicy


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "http://localhost/",
        "http://localhost./",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://127.000.000.001/",
        "http://0x7f.0.0.1/",
        "https://user:private@example.com/",
        "https://example.com:8443/",
        "https://internal.local/",
        "https://example.com\\@127.0.0.1/",
        "https://example.com/%ZZ",
        "https://example.com/%2e%2e/secret",
        "https://example.com/\nsecret",
        "https://example.com/ PRIVATE",
        "http://[fe80::1%25eth0]/",
    ],
)
def test_invalid_and_ssrf_targets_are_rejected_without_echoing_url(url):
    with pytest.raises(InvalidSourceURL) as error:
        normalize_url(url)
    assert str(error.value) == "invalid_source_url"


def test_normalization_deduplicates_fragments_ports_and_host_case_conservatively():
    assert normalize_url("https://EXAMPLE.COM.:443/%61rticle#top") == "https://example.com/article"
    assert normalize_url("https://example.com") == "https://example.com/"
    assert normalize_url("https://example.com/a?b=2&a=1") != normalize_url(
        "https://example.com/a?a=1&b=2"
    )
    assert normalize_url("https://example.com/a%2fb") == "https://example.com/a%2Fb"


@pytest.mark.parametrize(
    "addresses", [[], ["10.0.0.1"], ["93.184.216.34", "127.0.0.1"], ["169.254.169.254"], ["::1"]]
)
async def test_dns_validation_rejects_every_nonpublic_answer(addresses):
    async def resolve(host, port):
        return addresses

    with pytest.raises(InvalidSourceURL, match="source_dns_not_public"):
        await PublicSourceURLPolicy(resolver=resolve).validate("https://example.com/a")


async def test_public_dns_is_checked_again_and_rebinding_rejected():
    count = 0

    async def resolve(host, port):
        nonlocal count
        count += 1
        assert host == "example.com" and port == 443
        return ["93.184.216.34"] if count == 1 else ["10.0.0.1"]

    policy = PublicSourceURLPolicy(resolver=resolve)
    assert await policy.validate("https://example.com/a") == "https://example.com/a"
    with pytest.raises(InvalidSourceURL, match="source_dns_not_public"):
        await policy.validate("https://example.com/a")


async def test_dns_timeout_is_sanitized():
    async def resolve(host, port):
        await asyncio.sleep(0.05)
        return ["93.184.216.34"]

    with pytest.raises(InvalidSourceURL, match="source_dns_failed"):
        await PublicSourceURLPolicy(resolver=resolve, timeout_seconds=0.001).validate(
            "https://example.com/a",
        )
