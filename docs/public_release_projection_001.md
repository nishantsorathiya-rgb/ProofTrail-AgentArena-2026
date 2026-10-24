# Public selection and historical-record boundaries

Version: publication projection 001, 22 October 2026 IST. This is an explicit
public derivative of the reviewed 281-file tree, not a rewrite of the private
workspace or its historical records. No runtime Python, script, test, dependency,
migration or original organizer workflow is changed. All 423 existing tests and
all original fixture datasets remain selected, including the separate held-out set.

Optional historical phase reviews, live-provider journals, account observations
and duplicate validation archives are excluded. B002's demonstration expansion
is deferred; it is not needed to publish the complete code and offline evaluation.
The exact public paths are in release/public_allowlist_005.txt. Exclusion is a
publication decision, not a claim that an omitted run did not happen or failed.

## B001 excerpt and archive marker

results/p8_session_b_001_trace.json is labeled PUBLIC_REGRESSION_EXCERPT_V1 and
contains only the unchanged original question, planner assignment, candidate, four referenced complete
source snapshots and four exact passages. URLs, IDs, hashes, source content,
quote offsets, evidence attribution and provenance timestamps are copied exactly.
It contains no operational timing/credit records, raw provider payloads, model
usage log, audit timeline or historical final report. It must not be presented as
the full original trace. The retained source timestamps establish provenance;
they are not provider-latency observations.

results/p8_session_b_001_execution.json is explicitly an archive-retention marker,
not an execution journal or fabricated replacement measurement. The original raw
private B001 journal remains unchanged. The marker exists solely for the existing
test that checks an archive artifact is not modified by fixture launcher execution.
That public guard covers the marker; a separate preparation-time fingerprint
check covers the real private B001/B002 originals. This difference is disclosed.

results/p8_session_b_001_failure_analysis.json remains byte-identical. Its four
actual legacy invalid-coordinate quote cases still drive the same positive and
negative assertions. The synthetic offline_quote_locator trace is retained
unchanged. No verdict, evidence source, source hash or bad-coordinate assertion
was changed to obtain a passing suite.

## Frozen manifest impact

results/p8d_source_manifest.json is retained byte-identically as the original
162-file B002 preparation snapshot. Two public derivatives differ from that old
snapshot: README.md and results/p8_session_b_001_trace.json. All other referenced
inputs remain identical. No old hash has been silently refreshed. Consequently
the consumed B002 launcher's original manifest guard intentionally rejects this
public derivative before any provider request. It is not a newly authorized live
launcher. Its source-change guard tests and the main application workflow remain
unchanged. A future live run would need a separately reviewed fresh session and
manifest, not weakening or replaying B002.

## Rights and disclosure

The owner reserves only rights actually held in original project material. Python
documentation retains PSF source notices and complete applicable agreements.
Project-specific AI-assisted origins remain disclosed. The source publisher's
license is distinct from the retrieval service's contract. No actual Tavily
performance dataset is published in this selection. Current service terms and
the applicability of organizer entry/IP/reuse terms remain distinct owner review
items; preparing a narrower scope does not invent contractual permission.

The first publication-scope validation attempt omitted state.original_question,
causing six replay failures on each Python version. The exact original field was
restored. The failures remain in the release-validation record. No application
code, original test or assertion was changed to repair this excerpt.
