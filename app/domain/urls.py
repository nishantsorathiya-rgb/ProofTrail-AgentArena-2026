"""Conservative URL canonicalization without fetching or changing query semantics."""

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from app.domain.retrieval import InvalidSourceURL


def is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
    return address.is_global and not address.is_multicast


def normalize_url(value: str) -> str:
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ValueError
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        if "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError
        hostname = parsed.hostname
        if not hostname or "%" in hostname:
            raise ValueError
        hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        scheme = parsed.scheme.lower()
        if parsed.port not in {None, 443 if scheme == "https" else 80}:
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            labels = hostname.split(".")
            if len(labels) < 2 or labels[-1].isdigit() or len(hostname) > 253:
                raise ValueError from None
            if any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
            ):
                raise ValueError from None
            if labels[-1] in {
                "local",
                "localhost",
                "internal",
                "invalid",
                "test",
                "lan",
                "home",
                "onion",
            }:
                raise ValueError from None
        else:
            if not is_public_ip(hostname):
                raise ValueError
            hostname = f"[{address.compressed}]" if address.version == 6 else address.compressed
        path = parsed.path or "/"
        if re.search(r"%(?![0-9a-fA-F]{2})", path + parsed.query):
            raise ValueError

        def canonical_escape(match):
            char = chr(int(match[0][1:], 16))
            return (
                char
                if char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
                else match[0].upper()
            )

        path = re.sub(r"%[0-9a-fA-F]{2}", canonical_escape, path)
        if any(segment in {".", ".."} for segment in path.split("/")):
            raise ValueError
        return urlunsplit((scheme, hostname, path, parsed.query, ""))
    except (ValueError, UnicodeError):
        raise InvalidSourceURL("invalid_source_url") from None
