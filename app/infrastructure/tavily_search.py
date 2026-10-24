from uuid import UUID

from app.domain.ports import SourceURLPolicy
from app.domain.retrieval import InvalidSourceURL, RetrievalError, SearchHit, SearchResponse
from app.infrastructure.tavily_http import TavilyHTTPClient


class TavilySearchAdapter:
    def __init__(self, client: TavilyHTTPClient, urls: SourceURLPolicy):
        self._client, self._urls = client, urls

    async def search(self, query: str, *, run_id: UUID) -> SearchResponse:
        if not isinstance(query, str) or not query.strip() or len(query) > 400 or "\x00" in query:
            raise RetrievalError("invalid_search_query")

        async def parse(payload: dict) -> SearchResponse:
            rows = payload.get("results")
            if not isinstance(rows, list) or len(rows) > 5:
                raise RetrievalError("tavily_invalid_response")
            hits, seen, rejected = [], set(), 0
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                    raise RetrievalError("tavily_invalid_response")
                try:
                    normalized = await self._urls.validate(row["url"])
                except InvalidSourceURL:
                    rejected += 1
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                title = row.get("title") or ""
                snippet = row.get("content") or ""
                if not isinstance(title, str) or not isinstance(snippet, str):
                    raise RetrievalError("tavily_invalid_response")
                hits.append(SearchHit(row["url"], normalized, title[:500], snippet[:2000]))
            return SearchResponse(tuple(hits), rejected)

        return await self._client.post(
            "search",
            {
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
                "auto_parameters": False,
                "include_usage": True,
            },
            run_id=run_id,
            units=1,
            parse=parse,
        )
