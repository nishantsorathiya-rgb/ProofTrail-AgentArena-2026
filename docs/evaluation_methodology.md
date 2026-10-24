# P6 evaluation methodology

P7 policy update: the saved P6 measurements remain historical and unchanged. Current positive support additionally requires a blind semantic review and evidence-bound receipt; see `p7_semantic_integrity.md`. Current classifier runs provision two model requests per candidate and live case selection reserves both slots. This does not retroactively relabel P6 results or approve any case.

This framework distinguishes software behavior under controlled fixtures from independently reviewed semantic accuracy. P6 used no live model, search, extraction or source fetch requests. Synthetic evidence and mocked model output cannot establish Mesh accuracy, real research completeness, resistance to an actual successful model injection, or a performance improvement.

## Dataset construction and independence

`tests/evaluation/datasets/claims.v1.json` contains 60 draft claim/evidence pairs: 20 SUPPORTED, 20 CONTRADICTED, 20 INSUFFICIENT_EVIDENCE. Development contains 12 per class (36); held-out contains 8 per class (24). Ten synthetic organization/domain families were assigned to splits before execution, six development and four held-out. No domain family or identical evidence text crosses splits. These cases use simple numeric ledgers with period/entity/measure qualifiers, not authentic web snapshots. Their limited linguistic diversity is an explicit limitation.

Every row includes the required claim, exact evidence, fixture source identity, explicit synthetic provenance, proposed verdict/explanation, UNREVIEWED status, reviewer notes and split. There are **zero human-reviewed cases** at delivery. Generated labels are hypotheses for owner review, not ground truth. No organizer or external source provenance was invented. HTTP URLs under example.com used in the temporary database are synthetic source identifiers, never claims that those pages were visited. Seeded extraction records represent imported fixtures, not actual Tavily extraction requests; `evaluation_fixture_seeded` audit events say live_retrieval=0.

The original `Evaluation_Case_Template.csv` remains unchanged and still has zero approved populated examples. The new worksheet retains its concepts: atomic_claim -> claim_text; exact_evidence_passage -> evidence_text; source_url_or_fixture_id -> evidence_source; expected_label -> expected_verdict; review_notes -> reviewer_notes. Additional provenance, exclusion and reviewer fields avoid ambiguous approvals.

`adversarial.v1.json` is a **separate 18-scenario** synthetic suite, including a four-way conflicting-source case. Its labels also remain UNREVIEWED. It does not add cases to the three-class 60-case corpus. `workflows.v1.json` defines three complete question/scenario runs and two explicit expected subtopics. All coverage rubrics remain UNREVIEWED.

Default CLI execution selects development only. Held-out execution requires `--allow-held-out`. The supplied full offline artifact executes both splits under the same frozen controlled-record policy and unchanged production prompts; no prompt tuning or model selection used held-out predictions. Dataset, fixture-policy, agent-source, evaluation-code and prompt hashes are captured before either split executes. CI tests inspect split structure and exercise development examples plus the separate attack suite, not held-out semantic tuning. UUIDs, timestamps and local timings naturally vary between reruns; dataset hashes, labels, decisions and metric definitions remain reproducible.

## What actually executes

The evaluation composition root creates a temporary database and applies existing Alembic migrations through 0005. It seeds exact source snapshots/passages through the P2 repository, assigns a P3 research task, persists immutable P4 candidates and invokes the real VerifierAgent via BudgetedModel and MeshLLMAdapter. The Mesh SDK uses an explicit HTTP MockTransport offline. Each candidate has one model-request reservation; retries and graph research are not silently added to classifier evaluation.

The offline provider parses only the unchanged claim and persisted evidence in the actual verifier request. It receives no expected label, reviewer status, explanation or split. Its deliberately narrow controlled-sentence grammar can compare matching entity/period/measure records. This is a fixture interpreter, **not an independent semantic judge**. Agreement with generated labels only checks plumbing and controlled behavior. Fault modes deliberately emit wrong or malformed model output to exercise the actual rejection gates.

The verifier and SQL repository still check claim identity, evidence/run/task ownership, extraction status, hashes, stable passage IDs, exact source slices and absolute quote offsets. The evaluator separately inspects *proposed* citations, including references rejected by the verifier, so invalid model citations do not disappear from metrics. Every prediction preserves the actual verdict, operational outcome, rejection reason, exact quotations, attempt ID, model identifier, API/token metadata, timings, persistence round-trip result and real audit events.

## Eligibility and error accounting

Formal ground-truth metrics use only explicitly HUMAN_REVIEWED cases with a named reviewer. AMBIGUOUS and EXCLUDED cases are not scored. Exclusion requires a documented reason. Proposed-label agreement over eligible UNREVIEWED plus reviewed fixtures is reported separately under `provisional_fixture_agreement_NOT_model_accuracy`.

Errors are not semantic abstentions:

- Completed semantic INSUFFICIENT_EVIDENCE is an ordinary classifier prediction.
- Provider timeouts, malformed structured responses, exhausted model budgets and other operational failures occupy OPERATIONAL_ERROR.
- Rejected evidence/citation integrity occupies VALIDATION_REJECTED.
- Both error types remain in the accuracy denominator and cannot receive correct-class credit. Neither earns semantic unsupported-rejection credit. Structural fail-closed behavior is reported separately.
- CONFLICTING is a possible prediction column in the three-class matrix, never silently relabeled. Expected conflicts are evaluated only in the separate adversarial suite.

## Classification metrics

For each class c: TP is a completed correct prediction; FP is a completed c prediction with a different expected class; FN includes every c case not correctly predicted, including operational/integrity failures. Precision = TP / predicted-c count. Recall = TP / actual-c count. F1 = 2PR/(P+R), with 0 when P=R=0 and both are defined. Macro F1 is the unweighted mean of the three F1s **only when all three are defined**; otherwise null. A zero denominator is always null (N/A), never 100% or an invented perfect score.

Accuracy = completed correct predictions / all eligible cases, including failed cases in the denominator. The confusion matrix uses expected rows and prediction/error columns. JSON includes actual/predicted counts for each class.

Unsupported-claim rejection recall = completed non-SUPPORTED predictions / cases expected CONTRADICTED or INSUFFICIENT_EVIDENCE. Operational/integrity failures are separately safe failures, not successful semantic rejections. False-SUPPORTED count = completed SUPPORTED predictions on those non-supported cases. False-SUPPORTED rate = this count / all expected non-supported cases. Supported-assertion precision = correct SUPPORTED / all completed SUPPORTED predictions. These different denominators are explicit; low false-approval rate alone does not establish useful coverage.

## Structural citations versus semantic support

Each unique proposed evidence reference per prediction is checked for existing source/passage, exact quote text and offsets, retained/full hash agreement where recomputable, saved claim-version hash, run/link ownership and correct claim ID. Citation validity = references passing every structural check / all proposed references. Each component also has its own numerator/denominator. Missing references count invalid. No emitted references means N/A, not perfect citation integrity. Truncated full-source hashes cannot be independently recomputed from a retained prefix; that existing limitation remains.

A correct classification label also does **not** establish that each selected quotation semantically supports a claim. The initial semantic-citation correctness metric remains N/A until independent quote review. `p6_citation_owner_review.csv` supplies the unchanged claim, full evidence context, proposed exact quotations and structural checks for that review. Owner judgments SUPPORTS_CLAIM, CONTRADICTS_CLAIM or DOES_NOT_ESTABLISH are separate from class labels. The review command reports semantic support precision on owner-reviewed citations attached to SUPPORTED predictions; it does not infer judgments from a URL, matching words or model confidence. A multi-source claim may require joint context beyond any single quotation; individual-quote review does not replace reviewing the full claim/evidence pair.

The attack suite intentionally supplies two malicious SUPPORTED outputs for a negation reversal and a conditional statement. Both carry real, exact quotations with valid IDs. The production structural gates accept those outputs. They are recorded as false semantic approvals under proposed labels, demonstrating a remaining dependence on the semantic model. This is injected provider behavior, **not a measured Mesh false-approval rate**. Fabricated IDs, invented quotes, incorrect offsets and wrong claim IDs are tested separately against the real rejection logic.

## End-to-end and retry measurements

Three question variants run the actual P5 LangGraph under synthetic Mesh/Tavily HTTP transports: a recovery with novel revenue evidence, unsuccessful additional research, and empty initial/follow-up search. All roles, request reservations, tool calls, persistent evidence, retry edges and final report validation actually execute. These cases are narrow variants of one fictional organization, not broad real-world end-to-end coverage.

Expected subtopics are employee count and revenue amount, each with a required exact factual assertion. The evaluator records whether the plan contains the expected term (a **proxy, not a planning-quality score**), whether the exact fact was retrieved and whether it appears in supported final findings. Provisional coverage = matched expected factual subtopics / explicitly listed expected subtopics, **not number of citations**. Reviewed coverage remains null until the owner reviews the rubric. Human planning quality remains unmeasured. The report's own partial/incomplete status is retained even when provisional subtopic coverage is complete.

Groundedness checks that final finding IDs belong to the current integrity-revalidated supported set; rejected claims must be absent. Report reads and explicit report validation recheck actual source/citation integrity. Audit checks compare attempt start/finish IDs with persisted results, contiguous event sequences and provider counts with persisted budgets. These are structural/workflow checks, not an independent semantic-quality judgment.

Each retry preserves its initial verdict, trigger, assigned query, search/extract counts, new evidence IDs, new cited passage IDs, actual reverification output, final state and duration. `mechanical_recovery_observed` requires initial completed insufficiency -> completed support, an actually performed additional search and a cited novel passage whose text was absent from the prior evidence scope. Earlier support rechecked as support does not count as recovery. Failed/unresolved/empty retries do not count. **Human-confirmed successful retries remain zero** because the evidence/rubrics are unreviewed. Additional API usage is counted inside the assigned retry interval; follow-up planning calls are reported separately. Retry duration excludes preceding follow-up planning, and stage durations can overlap; they must not be added to infer total latency.

## Reproduction and interpretation

Use Python 3.12 and the existing pinned environment. From the repository root:

```powershell
& .\.venv\Scripts\python.exe -m scripts.evaluate_p6
& .\.venv\Scripts\python.exe -m scripts.evaluate_p6 --split all --allow-held-out
& .\.venv\Scripts\python.exe -m scripts.check_p6
```

The default output files are `results/p6_evaluation_offline.json`, `results/p6_metrics.md` and the quote-review worksheet. The JSON includes every selected prediction, separate split metrics, attack failures, full graph/report/audit traces, environment/dependency versions, prompt/policy/configuration hashes, API metadata and measured duration. No Git revision is invented when the repository has no commits. Preserve the JSON and its matching quote worksheet together; review IDs belong to that exact session.

Actual verification, persistence and graph-stage durations are measured locally. Estimated input tokens use the existing character-count heuristic; returned offline token/credit numbers are explicitly mock metadata, not Mesh consumption or billing. Live calls and actual API cost in the supplied offline session are zero. No baseline comparison or speedup claim is made; paired benchmarking belongs to P7.

An optional, separately gated `scripts.evaluate_p6_live` can later run the same persisted-evidence verifier against Mesh, with reviewed-case selection and a caller-specified hard request cap. It has **not been executed**. It does no Tavily retrieval, uses stored evidence snapshots and no model fallback/SDK retry. It refuses missing approval, unreviewed cases, accidental held-out selection and invalid caps. Independent source review, representative examples, private Mesh configuration, owner authorization and billing checks remain prerequisites. Actual live token metadata would be preserved; dollar cost would remain unknown unless independently checked. An approved live run on synthetic cases would measure only that synthetic domain, not general real-world accuracy.
