# ADR 0006 independent evaluation boundary

Status: P6 framework implemented for owner review. This is an architecture decision record, not a new Alembic revision; database head stays 0005.

Evaluation is an outer composition root under app/evaluation plus CLI scripts. It reuses domain records, VerifierAgent, BudgetedModel, MeshLLMAdapter and the existing SQL repositories. The HTTP API, graph, production agent prompts, retrieval adapters, schema and dependency manifest are unchanged. No evaluation labels are introduced into application/agent inputs.

The owner explicitly requests 36 development and 24 held-out cases, balanced across three classes. This refines the earlier handoff's minimum held-out count. The generated 60-case bundle is entirely synthetic and UNREVIEWED; no authentic source attribution or human review is invented. Domain families and identical evidence cannot cross splits. Default execution is development only; explicit held-out evaluation uses frozen policy/prompt hashes and never tunes prompts.

Formal reviewed metrics, provisional fixture agreement, structural citation integrity and independently reviewed semantic citation support are separate outputs. Operational and integrity failures occupy their own confusion-matrix columns, stay in the accuracy denominator, and cannot masquerade as correct insufficiency or successful semantic rejection. The old P4 evaluator's operational-error credit is corrected; that change affects evaluation only.

The actual model boundary and SQL verifier gates execute offline under HTTP mocks. The controlled-record interpreter and malicious-model scenarios live in scripts and versioned fixture data, not production agent logic. Actual P5 graph scenarios measure imported synthetic research coverage, new evidence and retry transitions without claiming live model accuracy. Deliberately wrong semantic decisions with structurally valid quotations remain visible limitations.

Owner worksheets are explicit, version-bound and never auto-approved. Claim labels and specific citation entailment require separate review. A future live classifier command uses the same Mesh boundary with explicit caller approval, reviewed-case selection, one invocation per candidate and a hard session cap; it is never invoked by CI or phase checks. No live calls were authorized or executed for this phase. P7 paired benchmarking and release remain outside this decision.
