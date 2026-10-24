# ADR 0001 ProofTrail foundation

Status: accepted as locked by the owner for P1 on 13 October 2026.

ProofTrail needs a local, reproducible foundation before retrieval or agent workflows can be developed. The project remains one Python 3.12 FastAPI deployable. API code calls application services, application services depend on domain ports, and infrastructure implements external interfaces. `app/main.py` composes these modules. Domain records use standard dataclasses and enums; configuration uses Pydantic Settings.

Runtime inference has one boundary, `MeshLLMAdapter`, using AsyncOpenAI exclusively at `https://api.meshapi.ai/v1`. A URL literal in settings and a request transport guard enforce this route. Real networking is disabled by default, explicit offline mock transports are permitted in tests, redirects and environment proxies are disabled, and SDK retry count is zero. Missing credentials, timeouts, rate limits and provider failures become sanitized domain exceptions with no fallback. The future application owns call budgets and any explicit backoff/retry policy; it must close the reused client.

SQLite with SQLAlchemy asyncio and aiosqlite stores research runs. Each operation owns a short transaction/session, UTC timestamps round-trip explicitly, and local file databases use WAL. Alembic revision 0001 is the schema authority; application startup applies versioned migrations through a synchronous connection inside SQLAlchemy's async bridge. Migration history is checked by Alembic. No automatic destructive downgrade is allowed. Future changes require new reviewed revisions and database backups.

Structured local logs use UTC, request UUIDs, route templates and elapsed time. Safe HTTP errors omit exception details and validation input. Logs intentionally omit keys, prompts, private question text, query strings and provider bodies. Uvicorn access logs are disabled in the documented command; project logs carry the safe request summary.

No agents, graph, retrieval adapters, direct URL fetcher, research HTTP endpoints, evidence tables, verification loop, evaluation results or submission workflow are implemented in P1. LangGraph, Tavily Search plus Extract, atomic exact-passage verification, bounded insufficient-evidence retry and supported-only synthesis remain locked future decisions. The eventual coordinator is single-process and must detect interrupted jobs; those behaviors belong to P5, not this persistence slice.

Dependencies use standard pip and one pyproject manifest. Direct dependencies are pinned to the tested versions; a derived requirements.lock constrains transitive packages to the tested Windows Python 3.12 environment. A fully hashed cross-platform lock is not claimed. Tests block external networking and use explicit SDK mock fixtures. The owner reviews the measured P1 outputs before P2 can begin.
