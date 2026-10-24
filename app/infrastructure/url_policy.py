import asyncio
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from app.domain.retrieval import InvalidSourceURL
from app.domain.urls import is_public_ip, normalize_url

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve_public_candidates(hostname: str, port: int) -> list[str]:
    rows = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return list({row[4][0] for row in rows})


class PublicSourceURLPolicy:
    def __init__(self, *, timeout_seconds: float = 3, resolver: Resolver | None = None):
        self._timeout = timeout_seconds
        self._resolver = resolver or resolve_public_candidates

    async def validate(self, url: str) -> str:
        normalized = normalize_url(url)
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        if is_public_ip(hostname):
            return normalized
        try:
            addresses = await asyncio.wait_for(
                self._resolver(hostname, 443 if parsed.scheme == "https" else 80),
                self._timeout,
            )
        except (OSError, TimeoutError):
            raise InvalidSourceURL("source_dns_failed") from None
        if not addresses or any(not is_public_ip(address) for address in addresses):
            raise InvalidSourceURL("source_dns_not_public")
        return normalized
