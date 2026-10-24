# P7 semantic integrity investigation and fix

P7 initially reproduced a **P0 release-blocking defect**: both P6 injected semantic errors reached `verified_findings` and passed final report validation. The initial evidence is preserved in `results/p7_semantic_initial.json` (the `--require-safe` command exited 1). It has not been overwritten or relabeled as a success. The owner then required fixing these defects before continuing benchmarking; subsequent measurement is gated on the corrected safety probes.

## Trace and root cause

| Stage | Negation reversal ADV-004 | Conditional assertion ADV-005 |
|---|---|---|
| Candidate | “In 2024, Test Archive did not record 42 records.” | “In 2024, Test Archive recorded 42 records.” |
| Exact source | Affirmative record of 42 records | The same statement under “Conditional only; if approved:” |
| P6 injected semantic response | Incorrect SUPPORTED | Incorrect SUPPORTED |
| Structural checks | Correct claim/run IDs, real exact quote, valid offsets, hashes and linkage | The same checks passed, including the conditional context |
| Old persistence | Stored the one model verdict as usable support | Stored the one model verdict as usable support |
| Old synthesis | Organized the already mislabeled supported claim | Organized the already mislabeled supported claim |
| Old final validation | Rechecked that stored approval and source integrity; accepted it | Rechecked that stored approval and source integrity; accepted it |

P6's classifier harness seeded unverified candidates directly and showed the first part of this failure. P7 extended that trace through actual synthesis, transactional report persistence and report reads. The corrected runner also includes **actual LangGraph Planner -> Researcher -> Drafter -> Verifier -> report** traces for both cases, so drafting is no longer just inferred from a manually seeded candidate.

Drafting was not an approval: malformed/unsupported candidates are permitted to exist as UNVERIFIED so the verifier can reject them. Both exact original claim strings remain unchanged in the corrected traces. The injected draft and primary semantic answers are explicit synthetic provider faults, not measured Mesh behavior.

The quote/hash validator was doing structural validation correctly. It could not decide whether an affirmative source entails its negation or whether a conditional statement proves an unconditional fact. The defect was the **approval policy**: a single semantic response, once structurally valid, was sufficient for usable support, and report validation trusted that response without a separately bound support review. Synthesis's closed vocabulary prevented new factual prose but could not correct a previously mislabeled claim.

## Implemented control

Within the existing Verifier role, a primary SUPPORTED decision now triggers a **second actual Mesh completion** under a separate blind support-review prompt. This is a fresh judgment using the same configured Mesh model. It is not a statistically independent model or human evaluator. The second request receives the unchanged claim and full bounded persisted evidence scope, without the primary verdict, rationale, quotation selection or confidence. It explicitly tests negation, conditions, modality, entity, units, number, date and omitted context. The application does **not** infer entailment with keyword rules.

Both responses are Pydantic-validated and checked against the persisted source passages. Primary invalid citations stop before review. A supported result is usable only when the separate review is also SUPPORTED and valid. A semantic review returning CONTRADICTED, INSUFFICIENT_EVIDENCE or CONFLICTING becomes the conservative final outcome; the original primary decision remains auditable. Timeout, malformed review, fabricated review references, cancelled/failed requests, missing review or exhausted request budget cannot fall back to the primary approval.

The server creates a typed `SupportReviewReceipt`, outside model-controlled JSON. It binds:

- The unchanged claim in both semantic decisions.
- Two distinct successful transactional model-usage IDs and their models.
- A fingerprint of all supplied persisted evidence, including exact text, URLs, IDs and hashes.
- Exact review quotes/offsets used in the final result, policy version and timestamp.
- A server audit event binding the receipt digest to that exact verification attempt.

New writes, usable-supported-set reads and final report validation enforce this receipt. One response cannot masquerade as two calls; a receipt copied to another attempt, missing its audit, bound to changed evidence, or lacking genuine successful usage records is not sufficient. LLM JSON cannot inject receipt fields into the primary/review schema. Both calls consume the existing shared request budget and run within the existing verification semaphore/deadline. Source integrity is rechecked before/after reasoning and transactionally at persistence as before.

The retry eligibility calculation now provisions both possible semantic passes for eligible claims before requesting more research. Default run budgets remain 12 Mesh calls, 6 searches and 10 fetch units; they were not silently expanded. More conservative validation can reduce retries or coverage under those limits. The normal offline recovery example still fits the default budget; tests that specifically exercise two complete unresolved retries explicitly provision 18 calls and assert the measured 15 used.

## Historical compatibility

No database table or column is added; Alembic head stays 0005. Receipts and primary decisions are optional when **parsing historical JSON**, so old attempts remain readable and auditable. An old SUPPORTED attempt without the new receipt is **not automatically grandfathered into usable support**. Its effective collection status becomes insufficient with a missing-review integrity reason; a formerly stored report relying on it is withheld on revalidation. Historical data is not rewritten and no paid re-verification runs automatically.

The HTTP API, graph boundaries and four agent roles remain. Report generation still accepts only current usable supported claims, and synthesis cannot introduce new facts, URLs or fabricated references. The final gate now checks semantic-review provenance in addition to structural citation integrity, rather than claiming that structural checks perform semantic reasoning.

## Evidence and regression coverage

The initial failing trace is retained alongside fixed/versioned traces. In the corrected traces, the primary model still says SUPPORTED for both attacks. The blind review says CONTRADICTED for the negation reversal and INSUFFICIENT_EVIDENCE for the conditional assertion. Neither enters verified findings. The traces include original candidates, both structured decisions, usage IDs, receipt, source IDs/quotes, audit events and final report output.

All original 18 adversarial scenarios remain unchanged. New tests exercise the complete report path for all 18, actual drafting for the two defects, synthesis introducing a new fact, review timeout/malformed output/fabricated citation, budget exhaustion between primary and review, absent historical receipts, single-call receipt substitution, changed evidence fingerprint and deleted review audit. Prior structural/SSRF/injection/ownership/quote/hash protections remain tested. Existing request-count and audit assertions were updated to assert the new required calls/events; no safety case was removed, skipped or marked xfail.

See `results/p7_checks.json` for actual final test counts and raw session paths. Earlier failed runs are preserved, including `results/p7_regression_first.txt`; they are not presented as final green results.

## Limits of the fix

The known single-pass bypass is closed under deterministic mocked regressions. **Universal semantic correctness is not proven.** The same model can make correlated errors in both passes, a live injection may defeat both prompts, and bounded or incomplete evidence may omit essential context. Receipt validation proves actual review execution and evidence binding, not that the model's semantic judgment is objectively correct. No confidence threshold or fixture score substitutes for independent review.

The owner explicitly kept P7 offline. No live LLM test, human ground-truth review or real-world semantic accuracy measurement was performed. Independent reviewed evaluation and approved live validation remain release requirements. An offline safety-probe pass is not production release approval or a guarantee that no future unsupported claim can ever be approved.
