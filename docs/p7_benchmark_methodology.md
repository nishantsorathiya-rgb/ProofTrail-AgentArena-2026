# P7 paired benchmark methodology

P7 compares actual application execution under **explicit synthetic HTTP fixtures**. It does not measure live Mesh accuracy, real-world competitive quality or human-reviewed semantic accuracy. The owner prohibited live Mesh/Tavily requests. The existing 60-case corpus remains unchanged and UNREVIEWED (20/20/20, 36 development/24 held-out); the held-out set was not used for P7 prompt tuning.

## Paired systems and fairness

The versioned `tests/evaluation/datasets/benchmark.v1.json` contains five proposed fictional AlphaCo scenarios: recoverable missing evidence, unresolved research, empty search, negation error, and conditional evidence. All scenarios and independent annotations remain UNREVIEWED. Five variants of one fictional company are deliberately limited and are not a diverse real-world benchmark. No scenario is silently dropped because the system fails or answers nothing.

**Baseline** runs the actual Planner/Researcher graph and actual claim Drafter, then presents the generated candidates as explicitly unverified basic-research assertions with their persisted evidence references. It has no claim verification, support-review pass, research retry or separate Synthesizer. It is an intentionally specified basic baseline, not every possible competing research product. Formatting is deterministic; there is no fabricated successful model synthesis.

**ProofTrail** runs the actual current P5 graph, including primary semantic verification, mandatory blind review of positive support, conditional corrective research, reverification, distinct Synthesizer and final transactional report validation. Ambiguous/rejected/review-failed claims cannot enter verified findings.

Each pair uses the identical question, initial original URLs/exact source text/content hashes, planner/drafter prompts, requested model ID, temperature=0 and no fallback or SDK retries. UUIDs/run-scoped passage IDs and retrieval timestamps differ between isolated databases; content and attribution equivalence is checked explicitly. The runner aborts if initial evidence or model configuration differs. Arms use separate databases, so one cannot silently benefit from the other's cache. Execution order alternates across pairs.

Both arms use the same configuration ceilings: 12 Mesh calls, 6 searches, 10 fetch units, 18 claims, 9 total tasks and at most 2 research retries where implemented. Baseline consumes fewer calls because it omits verification/retry/synthesis; that difference is measured, not hidden or called an equal-cost comparison. Both draft with the same input/output limits. The positive support review adds a real request and can reduce retries or coverage under the fixed global cap. Atomic reservations enforce the limit even under concurrent work. Extra retrieval for ProofTrail is reported separately from the shared initial evidence.

## Independent scoring

The evaluator does **not** use ProofTrail's verification verdict as its ground truth. A separate immutable proposed rubric maps complete claim text to annotated exact-evidence requirements and specifies incompatible/absent support and conditional context. It evaluates each arm against the passages that arm actually cites. New evidence can change the independent judgment for a missing-evidence claim; mere retrieval counts cannot. Unknown assertions remain unknown rather than receiving correct credit.

These annotations were authored for controlled fixtures and have not been human-reviewed. The quality object separates `provisional_synthetic_metrics`, `human_reviewed_metrics=null`, and `live_model_metrics=null`. Model receipt validation, structural citation integrity and this external proposed semantic judgment are distinct. A favorable fixture result cannot establish live semantic accuracy. Cases with injected primary model errors remain in the data; the support-review fixture responses are explicit simulated semantic decisions, not production keyword rules.

Metrics use all emitted factual assertions, not just those the system's own verifier calls correct:

- Supported/unsupported final-claim rates: externally proposed supported/non-supported assertions divided by all assertions emitted by that arm. Unknowns are recorded separately. An empty output has N/A assertion precision, not perfect precision.
- False-supported rate uses non-supported factual assertions over all emitted assertions. For the baseline this describes unverified assertions, not a verifier approval. The denominator is explicit to avoid conflating it with P6's non-supported-input false-approval denominator.
- Structural citation correctness checks persisted evidence integrity. Semantic citation correctness additionally requires the independent proposed rubric to support the full assertion with its cited context. Per-assertion weighting is used; repeated citations do not multiply credit.
- Research completeness is supported exact expected subtopics covered / explicitly enumerated expected subtopics. Empty outputs score zero coverage. Citation count does not define coverage.
- Retry effectiveness requires an actual new search/evidence scope, a new cited passage, changed insufficiency -> support, and independent proposed support from that passage. Human-confirmed retry success remains unmeasured. Unresolved, irrelevant, duplicate or empty retries do not count as recovery.
- End-to-end latency runs from submission through usable output/necessary revalidation with database bootstrap excluded for both arms. Raw stage/model/retrieval timings, request counts, cache hits and fixture token metadata are retained. Stage intervals can overlap and must not simply be added to derive total time.
- Actual API cost is $0 offline. Estimated live cost stays N/A because the account/model rates and live token consumption are unknown. A formula is recorded instead of invented dollars; mock token counts are not live estimates.

Aggregate claim rates pool assertion numerators/denominators. Completeness pools expected subtopic instances, including empty cases. Full pair results and failures are retained; there is no winner claim based only on answering fewer questions.

## Human review workflow

Use the unchanged existing 60-case dataset and the interactive local reviewer:

```powershell
& .\.venv\Scripts\python.exe -m scripts.p7_review_cases --worksheet owner_review.local.csv --reviewer 'OWNER NAME' --split development
```

Each case exposes the unchanged claim, exact evidence, explicit provenance, proposed label and reasoning. Controls accept, correct, mark ambiguous, exclude with a reason, skip or save/quit. Nothing is marked reviewed without an explicit owner decision and named reviewer. Existing worksheet versions are backed up before saving. The original corpus stays 20/20/20; genuine owner label corrections are retained and reported even if the reviewed subset becomes imbalanced, rather than falsifying labels to preserve balance.

Held-out review needs an explicit `--split held_out --allow-held-out` and stays separate from development diagnosis/tuning. Do not use held-out results to revise prompts or select models. The existing P6 worksheet importer and quote-level semantic review remain available. No review session was executed by Codex and zero repository cases have been auto-approved.

## Versioning and replay

```powershell
# Required checks; the safety replay must pass before benchmark/performance execution:
& .\.venv\Scripts\python.exe -m scripts.check_p7
# Reproduce only the P0 safety gate into a NEW file:
& .\.venv\Scripts\python.exe -m scripts.p7_semantic --require-safe --output results/MY_NEW_SEMANTIC_TRACE.json
# New immutable paired session:
& .\.venv\Scripts\python.exe -m scripts.p7_benchmark --run-directory results/p7_runs/MY_NEW_PAIR_SESSION
```

Raw run directories and per-pair JSON use exclusive creation. The phase checker archives previous canonical result JSON by content hash before publishing a new summary, and every summary points to its immutable raw directory. Initial unsafe traces, early regression failures, pre-optimization read samples and the initial benchmark remain preserved. Dataset, prompt/source, fixture version, configuration, model ID, environment, timestamps, durations, requests and failure examples are recorded. No Git revision or external provider success is invented.

## Performance interpretation

The observed six-passage read bottleneck was repeated passage/source/extraction/version-link SQL. The implementation now batches those reads within each session while preserving hash, run/task ownership, extraction success, source version, stable passage ID and exact slice checks. It does not cache integrity results across verification/report calls. Before/after microbenchmarks use nine identical-content reads, discarding the first warmup. Query counts and raw timings are preserved. A later-passage corruption regression ensures the optimization does not validate just the first passage.

Additional profiles measure the existing cache against repeated URLs and existing one-versus-three research concurrency with an explicit injected 30ms provider delay. These are measurements of existing mechanisms, not newly claimed optimizations or live network speedups. Stage and per-request timings help distinguish SQL/SDK overhead from modeled I/O. The final semantic policy uses more model requests than the initial single-pass implementation, so whole-workflow before/after differences cannot be attributed solely to SQL batching. No verification check was disabled to improve speed.
