# Owner review worksheet

The supplied 60 cases and their proposed labels are **UNREVIEWED synthetic drafts**. No case, label, source provenance or semantic citation has been approved as human-reviewed. The original project-specific blank template remains unchanged. The evaluation tools are ready for owner review; human-reviewed accuracy and live classifier evaluation remain unmeasured.

## Review the claim/evidence pairs

Copy `tests/evaluation/datasets/owner_review.v1.csv` to a new local version before editing. Preserve the versioned JSON and original worksheet. The CSV includes all 60 cases, exact evidence, explicit provenance, proposed labels, explanations, family and split. Review development first; keep held-out judgments separate and do not use them to tune prompts.

For each case, compare the full unchanged claim with the full supplied passage, including entity, quantity, time, negation and conditions. Correct `expected_verdict` and `explanation` if appropriate. SUPPORTED requires the complete assertion; CONTRADICTED requires explicit incompatible evidence; INSUFFICIENT_EVIDENCE covers missing or incomplete support. Disagreement across sources belongs in the separate conflict suite, not a forced three-class label.

When satisfied, explicitly set `review_status=HUMAN_REVIEWED` and enter your identity in `reviewer`. Document reasoning or corrections in `reviewer_notes`. If ambiguous, set AMBIGUOUS. If invalid, set EXCLUDED and provide `exclusion_reason`. Leave unfinished cases UNREVIEWED. The importer never supplies a reviewer identity or approves cases automatically.

The importer permits edits only to labels, explanations and review/exclusion fields. It rejects changed evidence, claims, provenance, case IDs or split fields; those require a new versioned JSON dataset and matching worksheet. It rejects duplicates and mismatched worksheet IDs. Corrected labels may change class balance; retain and report those changes rather than silently replacing difficult cases.

```powershell
# Example paths after you create and edit this copy yourself:
Copy-Item tests/evaluation/datasets/owner_review.v1.csv owner_review.local.csv
& .\.venv\Scripts\python.exe -m scripts.evaluate_p6 --review-worksheet owner_review.local.csv --output results/p6_owner_development.json --markdown results/p6_owner_development.md --citation-worksheet results/p6_owner_development_citations.csv
```

The example command still uses mocked HTTP semantics. HUMAN_REVIEWED labels make the ground truth eligible, but **offline fixture agreement still is not Mesh accuracy**. Excluded/ambiguous cases are omitted from execution/scoring and their dataset status remains recorded. Review realistic independent evidence snapshots before claiming representative accuracy. Do not relabel evidence provenance as authentic merely because a synthetic example is plausible.

## Review the actual quotations independently

The generated `results/p6_citation_owner_review.csv` is bound to the attempt/evidence IDs in `results/p6_evaluation_offline.json`. Copy both before another evaluation run. Compare each proposed quotation with the full source context and unchanged claim. Structural validity is provided separately; a genuine quote may still be irrelevant, conditional or misleading.

Set `semantic_judgment` to SUPPORTS_CLAIM, CONTRADICTS_CLAIM or DOES_NOT_ESTABLISH. Set HUMAN_REVIEWED and your reviewer identity only after checking it. Preserve unchanged claim/context/ID fields. Ambiguous or excluded quotations are not scored; exclusions require reasons. The exporter refuses to replace a worksheet with owner annotations.

```powershell
& .\.venv\Scripts\python.exe -m scripts.review_p6_citations --evaluation results/p6_evaluation_offline.json --worksheet citation_review.local.csv --output results/p6_citation_review_scored.json
```

Only individual citations attached to SUPPORTED predictions enter the semantic support-precision denominator. This explicit owner judgment is distinct from model verdict agreement. The two intentionally false semantic approvals in the saved P6 adversarial results illustrate the difference between structural and semantic validity. They are fault-injected model responses, not measured Mesh errors. The later [P7 regression tests](p7_semantic_integrity.md) verify that blind review withholds both claims from final verified findings.

## Review workflow coverage and retries

Review `tests/evaluation/datasets/workflows.v1.json`: the exact expected subtopics are currently UNREVIEWED. Confirm whether workforce count and revenue amount actually cover the question, add/correct subtopics in a new version where needed, and enter your reviewer identity only after review. Repeated citations do not expand coverage. The plan-term proxy is not an independent planning-quality score; manually assess focus, completeness, duplicates and relevance.

Inspect each workflow trace, final findings, exclusions, gaps and retry history. A successful research retry requires an actual new search, genuinely new relevant evidence, and a justified changed verdict; merely changing a model label or repeating an existing source does not qualify. The current artifact records mechanical recovery separately and claims **zero human-confirmed successful retries**.

## Optional live evaluation — not executed

After you have explicitly approved a bounded billable session and privately configured Mesh, the optional command supports a hard cap. Do not execute merely to inspect the project; `--help` is read-only. The current supplied dataset has no HUMAN_REVIEWED cases, so live evaluation refuses it.

```powershell
# ONLY after separate live-evaluation approval. Two slots cover primary and blind review:
& .\.venv\Scripts\python.exe -m scripts.evaluate_p6_live --approved-live --max-calls 2 --review-worksheet owner_review.local.csv --output results/p6_live_session_001.json
```

The flag records caller authorization; it cannot prove account credits or pricing. This command makes no Tavily calls and loads existing evidence snapshots. The selected IDs are deterministic reviewed cases in dataset order, capped before execution. Held-out live evaluation needs its separate flag and must not feed tuning. Existing output files are rejected before calls. Actual monetary billing remains owner-checked; unknown cost stays null.

P6 itself performed no live run, push, deployment, P7 benchmark or contest submission. Later integration and publication work does not approve this dataset or establish human-reviewed accuracy; review status remains explicit in each versioned worksheet.
