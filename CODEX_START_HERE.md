# CODEX_START_HERE.md | DeepDive: ProofTrail
**Final frozen Codex playbook · 13 October 2026 · DeepDive track**

> Put this file and `AGENTS.md` at the root of a **personal** repository folder `deepdive-prooftrail/`. Read `AGENTS.md` before starting. Execute **one phase at a time**, not the entire schedule in one prompt. The user has four full development days **17–20 October**.

## 1. Read me first: what we are building
The user asks a research question. Planner decomposes it; Researcher uses **real Tavily Search + Extract** to obtain full evidence passages; a drafter generates atomic claims linked to saved passage IDs; Verifier checks exactness and semantic support via **Mesh**; insufficient subquestions loop to Researcher once at most (budget permitting); Synthesizer emits only supported factual statements, with disputes/caveats separated. Source and decision logs must be inspectable. Every model call in the **application** uses Mesh API only.

## 2. Frozen product and engineering decisions
| Decision | Lock |
| --- | --- |
| Track | DeepDive; working self-verifying research analyst |
| Product | ProofTrail / Evidence Audit Dashboard |
| Architecture | Modern modular monolith, pragmatic Clean/Hexagonal boundaries |
| Runtime | Python 3.12, FastAPI, Pydantic, LangGraph |
| LLM | Mesh API gateway only (`https://api.meshapi.ai/v1`) |
| Search | Tavily Search, `include_answer=false`, `search_depth=basic`, 5 results/query initially |
| Content | Tavily Extract on selected URLs; preserve exact text/hash/URL and failures |
| Persistence | SQLite + SQLAlchemy; UTC; run/event/source/claim/verdict tables |
| Performance | Async HTTP/model I/O, semaphores, dedupe/cache, enforced budgets, stage timings |
| API | `GET /health`, `POST /research`, `GET /research/{id}`, `GET /research/{id}/audit` |
| Testing | pytest + Ruff + compileall; opt-in live API smoke and paired benchmark |
| Evaluation | 60 human-reviewed claim–evidence cases (20/20/20), separate conflict/injection tests |
| Comparison | Baseline vs ProofTrail on same initial evidence and model; record quality + cost + latency |
| Frontend | Simple Jinja2 inspection; no elaborate UI before functional core |
| Release | Public GitHub, README, workflow, GitHub secrets; owner approves submission |

## 3. Mandatory runtime behavior (acceptance contract)
- `POST /research` responds `202` with a stable `run_id`; progress inspectable via GET; unknown run -> 404; HTTP handlers do not wait for complete research.
- Real LangGraph sequence: `PLAN -> RESEARCH -> DRAFT -> VERIFY -> [INSUFFICIENT? RETRY_RESEARCH] -> SYNTHESIZE -> VALIDATE_REPORT`. Distinct prompts/tools and traceable outputs for four agents.
- Evidence stored as `source_id`, original URL, title, UTC retrieval time, extraction status, SHA-256 content hash, `passage_id`, exact text and locator. Search snippets alone are not evidence.
- Verifier verdicts: supported, unsupported, insufficient, conflicting. Unsupported claims rejected; conflicting caveated; retry iff insufficient + budget; supported citations validated again before final output.
- SQLite persists status/events, including incomplete/failed/interrupted runs. One in-process single-worker run coordinator is fine **only when documented as non-distributed**.
- Data/source/prompt injection controls, source-size limits, rate limits and strict budget caps must be actually executed and tested.
- Accuracy and speed are measured from real runs, never guessed. Offline fixtures are clearly distinguished from live web/Mesh executions.

## 4. Initial project layout
```text
deepdive-prooftrail/
  AGENTS.md
  CODEX_START_HERE.md
  README.md
  .gitignore                # ignores .env, SQLite files, keys, caches, .venv
  .env.example              # NO REAL SECRETS
  pyproject.toml            # one authoritative dependency manifest
  app/
    main.py
    api/                    # FastAPI routes, schemas, errors
    application/            # use-cases, run service, budgets, coordinator
    domain/                 # evidence, claims, decisions, protocols
    agents/                 # planner, researcher, verifier, synthesizer, graph
    infrastructure/         # mesh, tavily_search, tavily_extract, SQLite
    evaluation/             # evaluators, baseline, metrics
  tests/unit/
  tests/integration/
  tests/fixtures/
  eval_data/                # reviewed data, benchmark questions/snapshots
  docs/                     # ADRs, methodology, security, limitations
  results/                  # GENERATED test outputs and trace examples
  .github/workflows/        # official organizer CI when approved
```
Keep files reasonably small and preserve strict dependency direction (`api -> application -> domain`, external adapters implement domain/application ports). Don't create empty placeholder agents.

## 5. Environment and API configuration
```dotenv
# .env.example — copy into .env; keep .env private
MESH_API_KEY=YOUR_MESH_KEY_HERE
MESH_MODEL_ID=MODEL_ID_FROM_YOUR_MESH_DASHBOARD
MESH_BASE_URL=https://api.meshapi.ai/v1
TAVILY_API_KEY=YOUR_TAVILY_KEY_HERE
DATABASE_URL=sqlite:///./data/prooftrail.db
MAX_LLM_CALLS_PER_RUN=12
MAX_SEARCHES_PER_RUN=6
MAX_FETCHES_PER_RUN=10
MAX_CLAIMS_PER_RUN=18
MAX_RETRIES_PER_SUBQUESTION=1
MAX_PARALLEL_FETCHES=3
HTTP_TIMEOUT_SECONDS=12
MAX_SOURCE_TEXT_CHARS=12000
FETCH_MAX_BYTES=1500000
PAGE_CACHE_TTL_SECONDS=3600
ENABLE_LIVE_REQUESTS=false
```

**Local Windows PowerShell setup** (Codex may adapt `uv` commands if installed):
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m pytest -q
python -m compileall app
ruff check .
python -m uvicorn app.main:app --reload
# In second shell: Invoke-RestMethod http://127.0.0.1:8000/health
```
If the chosen `pyproject.toml` defines a different extra, update documentation and commands consistently. Choose **one** project/dependency management approach. Don't claim `uv` is configured unless it actually is.

## 6. Retrieval provider contract - must be real
1. Search through Tavily Search endpoint/SDK; `include_answer=false`, `search_depth=basic`, `max_results=5`, no AI-generated search summaries as final research.
2. Select up to the bounded number of candidate URLs and use Tavily Extract on their original URLs. Inspect success and `failed_results` independently: HTTP 200 may still contain per-URL extraction failures.
3. Save title, URL, fetch/extract timestamp, content checksum, exact cited passage with offset/locator if available; verify its exact presence in extraction output. If source text missing, mark insufficient rather than citing the search snippet.
4. Treat source text as untrusted; no prompt control from webpages. Confirm URL/provenance consistency and respect licensing/site/provider limits; request direct HTTP only through a defensively validated fetcher if implemented.
5. Bound external request concurrency and cache. No fake successful retrieval response outside explicitly labeled offline test fixtures.
6. Keep search provider replaceable. Exa is **only** backup if unavailable, and requires owner approval before spending or adding implementation.

Official docs: https://docs.tavily.com/documentation/api-reference/endpoint/search and https://docs.tavily.com/documentation/api-reference/endpoint/extract. Available Tavily free credits/pricing may change; verify dashboard before use.

## 7. Verification design and audit contract
- `claim`: `claim_id`, `subquestion_id`, claim text, evidence passage IDs, generation source, status, verifier rationale.
- Deterministic pre-gate: passage IDs exist; claim cites passages actually stored for the run; direct quotations appear in extracted source (normalizing whitespace only when appropriate); current verification input is bounded and prompt-injection-safe.
- Semantic gate: distinct Mesh Verifier evaluates whether passage **supports**, **contradicts**, **insufficiently supports**, or whether multiple passages **conflict** on same assertion. Store verdict and rationale with source refs. A model's own confidence is not evidence.
- Retry gate: route to Researcher again only if marked `insufficient` and `retry_count < MAX_RETRIES_PER_SUBQUESTION`, with specific missing evidence query; log a real conditional edge and run budgets.
- Final deterministic gate: only supported claims asserted as facts; citation references valid, cited excerpts present, unsupported/conflicting only in labeled caveat/rejection trail. A verifier cannot certify external truth—only source support.

Recommended demonstration: an obviously false numerical claim gets **rejected**; a missing evidence case prompts **one actual retry** and either finds support or stops with `insufficient` status; malicious page instructions are ignored.

## 8. Evaluation suite: real evidence of quality
**A. Verification classification:** target **60 manually reviewed cases**: 20 supported, 20 contradicted, 20 insufficient. `Evaluation_Case_Template.csv` is only a blank scaffold and **does not count** as a completed set. Each row needs a sourced or explicitly synthetic fixture, exact evidence passage, claim, expected label, reviewer approval, and split (`dev` or `held_out`). Set aside at least 15 held-out cases before prompt tuning. Keep an extra conflict suite (e.g., 6-10 cases) and separate prompt-injection/SSRF tests.

**B. Metrics:** confusion matrix and macro F1; per-label recall; unsupported rejection recall = correctly rejected contradicted cases / all contradicted cases; supported-assertion precision = true evidence-supported assertions / all supported assertions emitted; exact citation validation rate = valid citations / all citations emitted. All undefined denominators -> `N/A`, never 100%. Also compute answer coverage to prevent reporting great precision by withholding all answers.

**C. Paired competitive benchmark:** `baseline` and `prooftrail` must get identical question + initial evidence snapshot and use same Mesh model and temperature/settings. Baseline produces a report **without** verifier/retry; ProofTrail starts from same draft or evidence and runs verifier and optional targeted retry. Judge final claims under the *same* independent rubric. Record coverage, unsupported assertions, citation support, latency, LLM/search calls, token usage when available; show tradeoffs. Do not compare different dates or feeds without recording that limitation. Use at least 5 varied live questions only with owner-approved API budget; report sample size visibly.

**D. Artifact files:** `eval_data/claims_reviewed.jsonl`, `eval_data/benchmark_questions.jsonl`, `results/verification_metrics.json`, `results/benchmark_comparison.json`, `results/failure_cases.jsonl`, `docs/evaluation_methodology.md`. Generate `results/` **only by executing runners**. Don't pretend templates or offline mocks are scored live tests.

## 9. Day-by-day plan and acceptance gates
| Date | Available time | Deliverable | Hard gate |
| --- | --- | --- | --- |
| 13 Oct | Office planning + evening | P1 Foundation | FastAPI health, Mesh adapter, DB config, offline tests |
| 14 Oct | Evening | P2 Tavily retrieval | Real search/extract with source hash/passages; failure handling |
| 15 Oct | Evening | P3 LangGraph cooperation | Genuine distinct agents and draft claims linked to stored evidence |
| 16 Oct | Evening | P4 Verification skeleton | Support statuses, deterministic gates, failing/negative tests |
| **17 Oct** | **FULL DAY** | P5 Core workflow complete | Real conditional insufficient->research retry, rejected-claim demo, audit |
| **18 Oct** | **FULL DAY** | P6 Evaluation + security | 60 reviewed cases if feasible, scoring CLI, adversarial tests, fixed results |
| **19 Oct** | **FULL DAY** | P7 Paired benchmark + speed | Baseline comparison with same inputs; latency/usage; simple audit UI |
| **20 Oct** | **FULL DAY** | P8 Release + submission | Fresh run, verified GitHub CI, owner submits with time buffer |

**Release buffer:** Aim to have complete P0 scope by the evening of 18 Oct and enter submission on 20 Oct well before the published cutoff. Day 20 is not a new feature day.

## 10. PHASE PROMPTS — PASTE ONE AT A TIME INTO CODEX

### P1 — Foundation (13 October evening)
```text
Read AGENTS.md and CODEX_START_HERE.md in full, then inspect this existing repository. I am the project owner. Implement PHASE P1 ONLY for the DeepDive ProofTrail hackathon entry. Use Python 3.12, FastAPI, pragmatic modular monolith, typed Pydantic config, SQLite/SQLAlchemy, and async MeshLLMAdapter pointing exclusively to https://api.meshapi.ai/v1. Do not make live/paid Mesh requests.

Implement: (1) reproducible Windows PowerShell setup and pyproject.toml, .gitignore, .env.example, README; (2) FastAPI app + GET /health, structured logging/request ID, safe error handling; (3) Mesh adapter with configurable model/key from env, mocked transport tests proving only Mesh base URL is used; (4) SQLite bootstrap/migrations strategy and a persisted ResearchRun entity; (5) >=5 meaningful offline tests, Ruff, compileall and architecture ADR.

Run real commands/tests; show actual stdout summary (do not fabricate), file list, environment assumptions, any failure and cost usage (=zero live calls if true). Stop. Do not build later phases. Do not commit/push/deploy or purchase credits without permission.
```

### P2 — Primary Tavily search and original evidence (14 October)
```text
Read both repository guidance files and inspect P1 code. Implement P2 ONLY. Introduce SearchPort/ExtractPort interfaces and working Tavily Search + Extract adapters: search_depth=basic, max_results=5, include_answer=false, explicit timeouts, usage and error handling, bounded concurrency, duplicate URL normalization, cache TTL, and source/passages persisted in SQLite. Check per-URL extract failures even on HTTP 200. Preserve original URL/title, UTC extracted time, source content hash, exact passage ID and source locator; never cite snippets as evidence. Add SSRF-safe design and treat extracted HTML/text as untrusted. Offline tests mock the provider; expose only ONE opt-in live smoke command with no automatic paid calls. Run tests/compile/Ruff, report commands and results, stop.
```

### P3 — Four distinct agent roles, first LangGraph run (15 October)
```text
Read AGENTS.md + CODEX_START_HERE.md. Build genuine LangGraph typed state and working Planner, Researcher, Verifier stub only as explicit INCOMPLETE phase dependency, and Synthesizer draft restricted to UNVERIFIED preview (never claim completion before P4/P5). Alternatively wire planner/researcher/atomic-drafter only and keep final report disabled until full verifier exists. Use MeshLLMAdapter exclusively for any model calls, pass real evidence IDs through state, and persist run events. Each actual role must have distinct prompts/tools and observable state transitions; never create decorative agent classes or hardcoded decisions. End-to-end offline fixture run must demonstrate real graph nodes with no network; optional live mode only with owner approval. Run tests, report output/limitations, stop.
```

### P4 — Deterministic plus semantic verification (16 October)
```text
Read the guidelines, inspect P3. Implement P4 ONLY: atomic claims with persisted evidence IDs, deterministic pre-verification (source exists, exact cited passage present), separate Mesh-based Verifier with statuses supported/unsupported/insufficient/conflicting, reason and cited evidence. Reject unsupported from confirmed-claim set. Prepare tests where obviously false arithmetic/numeric claims are contradicted, missing evidence is insufficient, contradicting sources are flagged and source prompt-injection is ignored. Keep any incomplete research retry explicitly marked until P5; no assertion that loop works yet. Run all offline tests and code checks. Stop with actual output.
```

### P5 — Real retry, report, audit, status (17 October FULL DAY)
```text
Read the two guidance files and current implementation. Finish genuine LangGraph conditional branch from insufficient claim/subquestion -> targeted Researcher -> verifier, with max one retry per subquestion and global budgets. Store retry state and events, retry reason/queries, exhaustion result and timings; test that research really executes again rather than just logging a retry. Implement final Synthesizer using ONLY supported claims, split out conflicting/insufficient caveats; final deterministic citation-integrity check, JSON and Markdown report. Implement POST /research returning 202/run_id, GET status, GET audit. Document single-process local coordinator, interrupted-run recovery and bounds. Add negative and positive full-pipeline tests, run actual checks, show one rejected claim and one real executed retry, then stop.
```

### P6 — 60-case evaluation + security (18 October FULL DAY)
```text
Read docs and inspect P5. Implement a reproducible verification-evaluation runner with an independently reviewed corpus of up to 60 target cases: 20 supported, 20 contradicted and 20 insufficient, keeping >=15 held-out cases. Generate draft labelled examples if needed but mark PENDING_REVIEW until human approval: never count placeholders as validated. Create confusion matrix, class precision/recall/F1, unsupported-rejection recall, citation-integrity rate, assertion precision and answer-coverage metrics with explicit denominators. Create separate conflict and prompt-injection suites, plus SSRF/timeouts/429/budget/empty-source tests. Run offline tests, then opt-in live evaluation only with owner approval and call cap. Save actual scores and failures to results directory. STOP and report unreviewed gaps.
```

### P7 — Fair benchmark, speed profiling, audit UI (19 October FULL DAY)
```text
Read guidelines and inspect existing project. Build paired baseline-versus-ProofTrail runner: SAME question, initial evidence snapshot, Mesh model/settings and independent judge rubric; baseline does not verify/retry, ProofTrail does; preserve added search/call costs. Report citation-support precision, unsupported assertions, completeness/coverage, latencies, calls, tokens/credits and failure cases. No invented improvements or cherry-picked demos. Profile real stage timings and tune only measured bottlenecks while retaining correctness; add minimal evidence audit HTML page if all P0 tests pass. Run full offline suite and approved bounded real tests; generate score tables from observed results, mark unsupported claims, stop.
```

### P8 — Release and submission (20 October FULL DAY)
```text
Release gate only. Read all guidance and inspect code. Run fresh Windows install instructions and all actual compileall/pytest/Ruff/evaluation scripts. Review .gitignore, dependencies, security scan, provenance, documentation, model routing and live API safety. Prepare the organizer's ACTUAL workflow URL from the provided challenge brief, `.github/workflows/agent-arena-2026-checks.yml`, and ask the owner to enter GitHub Actions secrets MESH_API_KEY and SUBMISSION_TOKEN directly in GitHub (do not request them in chat). Confirm public repo/CI critical checks only from observed data. Prepare a brief 2–3 minute optional demo and submission checklist, but do not push/deploy/submit without human approval. Verify deadline on live dashboard; target submission with several hours buffer. Report commit SHA/repo link/CI actual status and any blocker, STOP.
```

## 11. Review form Codex must return each phase
```text
PHASE:
PASS / PARTIAL / FAIL acceptance criteria:
CHANGED FILES:
COMMANDS ACTUALLY RUN + RESULTS:
OFFLINE FIXTURES vs REAL SEARCH/MESH:
REAL TIMING / TOKENS / API COST (or NOT MEASURED):
EXAMPLE OUTPUT + CLAIM/SOURCE TRACE:
KNOWN SECURITY / RELIABILITY RISKS:
NEXT PHASE SUGGESTION:
STOPPED FOR OWNER REVIEW: YES/NO
```

## 12. Challenge CI and submission
Supplied brief lists: public GitHub submission; Python/FastAPI backend; Mesh-only AI calls; a dependency manifest, README, `.gitignore`; GitHub secrets `MESH_API_KEY`, `SUBMISSION_TOKEN`; organizer workflow downloaded from:

`https://careerapi-production.krishnaik.in/api/ci/hackathons/agent-arena-2026/workflow.yml`

Save it as `.github/workflows/agent-arena-2026-checks.yml`. Critical automated checks reportedly include Python syntax, dependency manifest, Mesh routing and Mesh key validation. Check organizer's live site for current policies, deadlines, eligibility, free-credit offers and AI assistance rules. Screenshot in supplied document (not live-confirmed) suggests registration closes 17 Oct and submission closes **20 Oct 2026 at 23:00 IST**. Results/payment are **not guaranteed**.

## 13. Scope triage if behind schedule
- **Never cut:** genuine specialized agents, Mesh routing, real source extraction, exact support checks, actual insufficiency->research loop, audit events, self-evaluation, reproducible runnable GitHub repo, contest CI.
- **Cut first:** PDF export, SSE, polished landing page, image generation, Exa backup, extra dashboard charts, cloud infrastructure, long-tail topic support, more agents.
- **Operationally:** P5 unfinished by late 17 Oct -> freeze UI and extras. P6 unreviewed by late 18 Oct -> reduce counted evaluation cases and report honest completed N (60 stays target); prioritize 15 held-out + real negative tests. 19 Oct morning: freeze features, fix P0 blockers. 20 Oct: no architecture rewrites.

## 14. Official sources and important assumptions
- Organizer brief: original user-supplied `Doc1.docx`, pages 1–21. Organizer's dashboard is authoritative for deadline, registration and prize.
- Tavily docs: https://docs.tavily.com/documentation/api-reference/endpoint/search ; https://docs.tavily.com/documentation/api-reference/endpoint/extract ; pricing https://www.tavily.com/pricing
- LangGraph: https://docs.langchain.com/oss/python/langgraph/workflows-agents
- Mesh base URL documented by challenge: https://api.meshapi.ai/v1 ; model ID/credits require dashboard check.

**This is a product requirements / build handoff, not a claim that the app, tests, evaluation, contest submission or live credentials are already complete.**
