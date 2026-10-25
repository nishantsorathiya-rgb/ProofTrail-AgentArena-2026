# Versioned P6 evaluation data

- `datasets/claims.v1.json`: 60 synthetic UNREVIEWED draft cases, 20 per class; 36 development, 24 held-out.
- `datasets/owner_review.v1.csv`: matching owner worksheet, no approved rows.
- `datasets/adversarial.v1.json`: separate 18 synthetic attacks/fault-injection scenarios, including conflicting evidence.
- `datasets/workflows.v1.json`: three actual-graph fixture scenarios and two explicit UNREVIEWED expected subtopics.
- `datasets/benchmark.v1.json`: five synthetic paired-benchmark scenarios with UNREVIEWED annotations.

Generated labels are not human ground truth. Synthetic fixture identities are not source URLs or live provenance. `scripts.prepare_p6_fixtures` can create a **new** draft directory and refuses to overwrite existing files. Do not regenerate a reviewed dataset or tune on held-out examples. Fixture-provider logic does not receive labels; its controlled grammar is not a semantic model.

Run `python -m scripts.evaluate_p6` for default development-only measurements. Explicit `--split all --allow-held-out` evaluates both splits without tuning. Unit/integration tests exercise the actual verifier, SQL persistence, source/citation gates, budgeting, isolated prompts and P5 graph under mocks. Semantic classification failures deliberately injected into a provider are preserved in evaluation output, not disguised as framework test failures.

See [evaluation methodology](../../docs/evaluation_methodology.md) and [owner review](../../docs/evaluation_owner_review.md) for review eligibility, denominators, provenance, quote-level semantic review, replay and live-call guards. The [P7 benchmark methodology](../../docs/p7_benchmark_methodology.md) documents the separate paired fixtures and their limitations.
