# ADR 0008 — support Python 3.11 and 3.12 without changing runtime architecture

Status: owner-approved scope P8A.1; validation evidence is in `docs/p8_python_compatibility_review.md` and the two interpreter-specific result files.

The owner explicitly supersedes the original Python-3.12-only requirement to resolve the organizer workflow's Python 3.11 syntax incompatibility. All other locked architecture, provider routing, verification/security and phase boundaries remain. The organizer workflow itself must remain byte-for-byte unchanged.

Replace PEP 695 `async def post[T]` with module-level `T = TypeVar("T")` and ordinary `async def post`. The parser callback and return value retain the same `T` relationship. The HTTP method body is unchanged, including retries, response-size limits, rate handling, route checks, semaphore, atomic budgets, usage logging and exception mapping.

Before broadening the project requirement, the existing 62 version pins plus setuptools 82.0.1 were installed and checked in a clean Python 3.11 environment, including Requires-Python metadata, pip check and key application/provider imports. No dependency version was changed. `requires-python` becomes `>=3.11,<3.13`; Ruff targets py311 to avoid reintroducing 3.12-only upgrades. Setup/validation guards accept both supported versions; the legacy PowerShell `-Python312` parameter remains an alias. Default setup still prefers 3.12, with an explicit interpreter option for 3.11.

The separate manual offline workflow gains a 3.11/3.12 matrix. This is future CI configuration, not observed GitHub Actions success. Local validation must preserve all 317 existing test node IDs and run compilation, Ruff, dependency consistency and Alembic checks under both interpreters. Other Python versions and operating systems are not certified by these measurements.

Frozen AGENTS.md/CODEX_START_HERE.md and historical phase results remain unchanged. The new owner decision and measured validation, rather than silent edits to those historical instructions, establish the current supported interpreter range. Live provider compatibility, independent human-reviewed accuracy, actual remote CI and submission readiness remain separate pending gates.
