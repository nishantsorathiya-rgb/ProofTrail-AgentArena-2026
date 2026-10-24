# ADR 0007 — bound positive support to a blind semantic review

Status: P7 implementation for owner review. The owner explicitly classified the two P6 false semantic approvals as P0 defects and required fixing them before further benchmark execution. No architecture redesign or new provider/dependency is introduced.

A single model support verdict plus structural citation validation was insufficient: both injected errors reached verified findings and survived final report revalidation. The Verifier role now performs a second actual Mesh call only for positive primary support, under a separate blind prompt receiving the unchanged claim and complete bounded evidence but no primary decision/confidence. It uses the same configured model; statistical independence is not claimed. No keyword entailment rule replaces semantic reasoning.

Positive support requires a typed server receipt linking both validated decisions, distinct successful model-usage IDs, exact evidence fingerprint and an attempt-bound audit digest. New writes, supported-set reads and final report validation enforce this policy. Disagreement or unavailable/invalid review withholds support. Primary decisions remain in immutable history. Primary structured output cannot forge receipt metadata.

BudgetedModel decorates LLMResult with its actual transactional usage ID. Both completions share existing request limits, semaphore and deadline. Default budgets are unchanged, and retry planning provisions the extra semantic cost. Tests assert new real counts and continue testing fail-closed budget enforcement. More validation may reduce coverage/retries under the default budget; the benchmark records that cost rather than silently expanding production limits.

The receipt is optional for parsing legacy JSON but mandatory for currently usable SUPPORTED outcomes. Old approvals without a receipt remain auditable and are withheld on current reads/report validation, with no automatic paid re-verification or data rewrite. Existing SQL/audit/usage tables suffice; Alembic remains 0005. The graph and API continue using the existing agent roles/endpoints.

SQL read batching is a separate measured optimization: session-local bulk reads replace per-passage lookups without removing integrity checks or carrying trusted caches across calls. Raw failing/fixed traces and before/after profiles are preserved.

Same-model correlated semantic errors and real provider compatibility remain unmeasured without approved live evaluation and human-reviewed evidence. Passing offline regressions fixes the demonstrated single-pass path; it does not establish universal entailment or authorize production release. The owner keeps P7 offline and P8 remains unapproved.
