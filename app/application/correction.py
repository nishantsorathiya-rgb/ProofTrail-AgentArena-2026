"""Correction eligibility is application policy; source/model text cannot change budgets."""

from uuid import UUID, uuid5

from app.domain.claims import ClaimCollection, Verdict
from app.domain.workflow import ResearchGap, WorkflowState


def root_task_id(state: WorkflowState, task_id: UUID) -> UUID:
    task = next(t for t in state.subquestions if t.task_id == task_id)
    return task.parent_task_id or task.task_id


def detect_gaps(state: WorkflowState, collection: ClaimCollection) -> tuple[ResearchGap, ...]:
    gaps = []
    for root in (t for t in state.subquestions if t.parent_task_id is None):
        records = [
            r
            for r in collection.claims
            if root_task_id(state, r.candidate.originating_research_task_id) == root.task_id
        ]
        groups = {}
        for record in records:
            result = record.attempts[-1].result if record.attempts else None
            status = record.verification_status
            if record.current_integrity_error or result is None or result.outcome != "completed":
                kind, retryable = "operational_error", False
            elif status == Verdict.CONTRADICTED:
                kind, retryable = "contradicted", False
            elif status == Verdict.CONFLICTING:
                kind, retryable = "conflicting", False
            elif status == Verdict.INSUFFICIENT_EVIDENCE:
                kind, retryable = "insufficient_support", True
            elif status == Verdict.SUPPORTED:
                continue
            else:
                kind, retryable = "operational_error", False
            groups.setdefault((kind, retryable), []).append(record.candidate.claim_id)
        if not records:
            family = [
                t for t in state.subquestions if (t.parent_task_id or t.task_id) == root.task_id
            ]
            has_evidence = any(t.passage_ids for t in family)
            recoverable = any(
                t.status == "empty"
                or t.error_code
                in {
                    "tavily_timeout",
                    "tavily_connection_failed",
                    "provider_extraction_failed",
                    "extraction_result_missing",
                    "extraction_content_missing",
                }
                for t in family
            )
            kind = "incomplete_coverage" if has_evidence else "missing_evidence"
            groups[(kind, has_evidence or recoverable)] = []
        for (kind, retryable), claim_ids in groups.items():
            stop = None
            count = sum(r.root_task_id == root.task_id for r in state.retry_history)
            # Conservatively provision both semantic passes for each eligible existing claim.
            # An extra search must not be started assuming structural validity grants support.
            eligible = [
                r
                for r in records
                if r.verification_status in {Verdict.SUPPORTED, Verdict.INSUFFICIENT_EVIDENCE}
                and r.attempts
                and r.attempts[-1].result
                and r.attempts[-1].result.outcome == "completed"
            ]
            required_calls = 1 + 2 * max(1, len(eligible)) + (0 if records else 1)
            if not retryable:
                stop = {
                    "contradicted": "contradicted_not_researched",
                    "conflicting": "conflict_preserved",
                }.get(kind, "operational_error")
                if kind == "operational_error" and any(
                    r.candidate.claim_id in claim_ids
                    and r.attempts
                    and r.attempts[-1].result
                    and r.attempts[-1].result.rejection_reason == "model_budget_exhausted"
                    for r in records
                ):
                    # Preserve the actual cause when the budget ends between primary and review.
                    stop = "model_budget_exhausted"
            elif state.status == "failed":
                stop = "workflow_failed"
            elif state.correction_stopped:
                stop = state.correction_stop_reason or "correction_stopped"
            elif count >= state.budgets.max_retries_per_subquestion:
                stop = "retry_limit_reached"
            elif len(state.subquestions) >= state.budgets.max_research_tasks:
                stop = "research_task_budget_exhausted"
            elif not records and len(collection.claims) >= state.budgets.max_claims:
                stop = "claim_budget_exhausted"
            elif state.budgets.model_attempts + required_calls > state.budgets.max_llm_calls:
                stop = "model_budget_exhausted"
            elif state.budgets.search_requests >= state.budgets.max_searches:
                stop = "search_budget_exhausted"
            elif state.budgets.fetch_units >= state.budgets.max_fetches:
                stop = "fetch_budget_exhausted"
            gaps.append(
                ResearchGap(
                    gap_id=uuid5(root.task_id, f"gap:{kind}"),
                    root_task_id=root.task_id,
                    kind=kind,
                    question=root.assignment.question,
                    claim_ids=tuple(claim_ids),
                    reason=kind,
                    retryable=retryable and stop is None,
                    stop_reason=stop,
                )
            )
    return tuple(gaps)
