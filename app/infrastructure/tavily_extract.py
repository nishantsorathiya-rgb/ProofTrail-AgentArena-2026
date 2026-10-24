from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from app.domain.ports import SourceURLPolicy
from app.domain.retrieval import ExtractedPage, InvalidSourceURL, RetrievalError
from app.infrastructure.tavily_http import TavilyHTTPClient


class TavilyExtractAdapter:
    def __init__(self, client: TavilyHTTPClient, urls: SourceURLPolicy):
        self._client, self._urls = client, urls

    async def extract(self, urls: list[str], *, run_id: UUID) -> tuple[ExtractedPage, ...]:
        if not 1 <= len(urls) <= 20:
            raise RetrievalError("invalid_extract_batch")
        requested = {}
        for original in urls:
            normalized = await self._urls.validate(original)
            requested.setdefault(normalized, original)

        async def parse(payload: dict) -> tuple[ExtractedPage, ...]:
            successful, failed = payload.get("results"), payload.get("failed_results", [])
            if not isinstance(successful, list) or not isinstance(failed, list):
                raise RetrievalError("tavily_invalid_response")
            if len(successful) + len(failed) > 40:
                raise RetrievalError("tavily_invalid_response")
            matches: dict[str, list[tuple[dict, bool]]] = {}
            for rows, is_failure in ((successful, False), (failed, True)):
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                        raise RetrievalError("tavily_invalid_response")
                    try:
                        normalized = await self._urls.validate(row["url"])
                    except InvalidSourceURL:
                        raise RetrievalError("provider_provenance_mismatch") from None
                    if normalized not in requested:
                        raise RetrievalError("provider_provenance_mismatch")
                    matches.setdefault(normalized, []).append((row, is_failure))
            timestamp = datetime.now(UTC)
            pages = []
            for normalized, original in requested.items():
                rows = matches.get(normalized, [])
                error = None
                content = None
                digest = None
                size = 0
                if not rows:
                    error = "extraction_result_missing"
                elif len(rows) != 1:
                    error = "extraction_result_ambiguous"
                elif rows[0][1]:
                    error = "provider_extraction_failed"
                else:
                    text = rows[0][0].get("raw_content")
                    if not isinstance(text, str) or not text.strip() or "\x00" in text:
                        error = "extraction_content_missing"
                    else:
                        digest, size = sha256(text.encode()).hexdigest(), len(text)
                        content = text[: self._client.settings.max_source_text_chars]
                        if not content.strip():
                            content, error = None, "extraction_content_missing"
                pages.append(
                    ExtractedPage(original, normalized, timestamp, content, digest, size, error)
                )
            return tuple(pages)

        return await self._client.post(
            "extract",
            {
                "urls": list(requested.values()),
                "extract_depth": "basic",
                "format": "text",
                "include_images": False,
                "include_usage": True,
                "timeout": min(60.0, max(1.0, self._client.settings.http_timeout_seconds)),
            },
            run_id=run_id,
            units=len(requested),
            parse=parse,
        )
