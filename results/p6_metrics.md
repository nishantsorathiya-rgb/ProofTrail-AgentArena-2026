# P6 offline fixture evaluation

These are synthetic fixtures and mocked model responses. They do not measure Mesh semantic accuracy or real-world research quality.

Dataset: `prooftrail-synthetic-v1`; split: all.
Human-reviewed scored cases: **0**. Reviewed accuracy: **N/A**.
Live requests: **0**. Actual API cost: **$0**.

## Provisional controlled-fixture agreement (unreviewed labels)

Cases: 60; accuracy/agreement: 1.0; macro F1: 1.0.
False SUPPORTED: 0 / 40 non-supported cases.

| Proposed class | Precision | Recall | F1 | Cases |
|---|---:|---:|---:|---:|
| SUPPORTED | 1.0 | 1.0 | 1.0 | 20 |
| CONTRADICTED | 1.0 | 1.0 | 1.0 | 20 |
| INSUFFICIENT_EVIDENCE | 1.0 | 1.0 | 1.0 | 20 |

## Confusion matrix

Expected \ predicted; errors are separate columns.

| Expected | SUPPORTED | CONTRADICTED | INSUFFICIENT_EVIDENCE | CONFLICTING | OPERATIONAL_ERROR | VALIDATION_REJECTED |
|---|---:|---:|---:|---:|---:|---:|
| SUPPORTED | 20 | 0 | 0 | 0 | 0 | 0 |
| CONTRADICTED | 0 | 20 | 0 | 0 | 0 | 0 |
| INSUFFICIENT_EVIDENCE | 0 | 0 | 20 | 0 | 0 | 0 |

## Separate adversarial/fault-injection suite

Scenarios: 18. Invalid proposed citations: 4.
Operational errors: 3; validation rejections: 6.
Two deliberately malicious semantic responses use real quotations to falsely approve negated/conditional claims. Citation gates accept those structurally valid responses. This measures the limits of deterministic gates; it is not a measured Mesh failure rate.

| Failure case | Outcome | Verdict | Reason |
|---|---|---|---|
| ADV-004 | completed | SUPPORTED | wrong injected semantic decision |
| ADV-005 | completed | SUPPORTED | wrong injected semantic decision |
| ADV-006 | validation_rejected | INSUFFICIENT_EVIDENCE | invalid_verification_citation |
| ADV-007 | validation_rejected | INSUFFICIENT_EVIDENCE | verification_quote_mismatch |
| ADV-008 | validation_rejected | INSUFFICIENT_EVIDENCE | verification_quote_mismatch |
| ADV-009 | validation_rejected | INSUFFICIENT_EVIDENCE | verification_claim_mismatch |
| ADV-014 | operational_error | INSUFFICIENT_EVIDENCE | invalid_verifier_response |
| ADV-015 | operational_error | INSUFFICIENT_EVIDENCE | mesh_timeout |
| ADV-016 | operational_error | INSUFFICIENT_EVIDENCE | model_budget_exhausted |
| ADV-017 | validation_rejected | INSUFFICIENT_EVIDENCE | evidence_content_hash_mismatch |
| ADV-018 | validation_rejected | INSUFFICIENT_EVIDENCE | evidence_id_not_found |

## Actual workflow fixture measurements

Expected subtopics are UNREVIEWED. Coverage below is a provisional exact-fact rubric, not a citation-count proxy or reviewed real-world completeness score.

| Case | Fixture coverage | Total seconds | LLM | Search | Extract | Audit |
|---|---:|---:|---:|---:|---:|---|
| E2E-001 | 1.0 | 2.387466 | 9 | 2 | 2 | True |
| E2E-002 | 0.5 | 3.470559 | 12 | 3 | 3 | True |
| E2E-003 | 0.0 | 1.213884 | 3 | 3 | 0 | True |

Evaluation wall duration: 29.586105 seconds. Local fixture timings include SQLite/SDK/graph work; no live speedup is claimed.

JSON preserves all predictions, failure examples, exact quotes, hashes, offsets, attempt IDs, state/audit traces, retries and usage. See evaluation_methodology.md for denominators, review gates and limitations.
