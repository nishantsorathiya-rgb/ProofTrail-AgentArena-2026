"""Actual LangGraph correction-node implementations, reusing existing retrieval and verifier."""

from hashlib import sha256
from time import perf_counter
from uuid import uuid5

from app.agents.drafter import ClaimDrafter
from app.agents.followup_planner import FollowupPlanner
from app.agents.researcher import ResearcherAgent
from app.agents.verifier import VerifierAgent
from app.application.correction import detect_gaps, root_task_id
from app.domain.claim_ports import ClaimStore
from app.domain.claims import Verdict
from app.domain.llm import ModelUnavailable
from app.domain.workflow import (
    ResearchRetry,
    ResearchTask,
    SourceReference,
    Subquestion,
    WorkflowError,
    WorkflowState,
    utc_now,
)
from app.domain.workflow_ports import WorkflowStore


def channels(state):
    return {name: getattr(state, name) for name in type(state).model_fields}


class SelfCorrection:
    def __init__(
        self,
        planner: FollowupPlanner,
        researcher: ResearcherAgent,
        drafter: ClaimDrafter,
        verifier: VerifierAgent,
        claims: ClaimStore,
        store: WorkflowStore,
    ):
        self._planner, self._researcher, self._drafter = planner, researcher, drafter
        self._verifier, self._claims, self._store = verifier, claims, store

    async def decide(self, state: WorkflowState):
        state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
        collection = await self._claims.collection(state.run_id)
        gaps = detect_gaps(state, collection)
        state = state.evolve(
            execution_status="deciding",
            research_gaps=gaps,
            eligible_gap_ids=tuple(g.gap_id for g in gaps if g.retryable),
            active_retry_ids=(),
        )
        state = await self._store.save(
            state,
            agent="graph",
            event="verification_decision",
            details={
                "retryable_gap_ids": [str(g.gap_id) for g in gaps if g.retryable],
                "stop_reasons": sorted({g.stop_reason for g in gaps if g.stop_reason}),
                "unresolved_gap_count": len(gaps),
            },
        )
        return channels(state)

    async def plan(self, state: WorkflowState):
        started = perf_counter()
        state = await self._store.save(
            state.evolve(execution_status="planning_retry"),
            agent="planner",
            event="followup_planner_started",
        )
        gaps = tuple(g for g in state.research_gaps if g.gap_id in state.eligible_gap_ids)
        limit = min(
            len(gaps),
            state.budgets.max_research_tasks - len(state.subquestions),
            state.budgets.max_searches - state.budgets.search_requests,
            state.budgets.max_fetches - state.budgets.fetch_units,
        )
        collection = await self._claims.collection(state.run_id)
        try:
            if limit <= 0:
                raise WorkflowError("research_budget_exhausted")
            followups = await self._planner.plan(
                state.run_id,
                gaps,
                [r.candidate for r in collection.claims],
                [t.assignment.search_query for t in state.subquestions],
                limit,
            )
            if not followups:
                raise WorkflowError("no_useful_followup")
            retries, tasks = [], []
            for followup in followups:
                gap = next(g for g in gaps if g.gap_id == followup.gap_id)
                root = gap.root_task_id
                number = sum(r.root_task_id == root for r in state.retry_history) + 1
                retry_id = uuid5(root, f"retry:{number}")
                task_id = uuid5(retry_id, "research_task")
                affected = [
                    r
                    for r in collection.claims
                    if root_task_id(state, r.candidate.originating_research_task_id) == root
                    and r.verification_status in {Verdict.SUPPORTED, Verdict.INSUFFICIENT_EVIDENCE}
                    and r.attempts
                    and r.attempts[-1].result
                    and r.attempts[-1].result.outcome == "completed"
                ]
                tasks.append(
                    ResearchTask(
                        task_id=task_id,
                        parent_task_id=root,
                        retry_number=number,
                        assignment=Subquestion(
                            **{k: getattr(followup, k) for k in Subquestion.model_fields}
                        ),
                    )
                )
                retries.append(
                    ResearchRetry(
                        retry_id=retry_id,
                        root_task_id=root,
                        task_id=task_id,
                        number=number,
                        trigger=gap.kind,
                        question=followup.question,
                        search_query=followup.search_query,
                        claim_ids=tuple(r.candidate.claim_id for r in affected),
                        previous_verdicts={
                            str(r.candidate.claim_id): r.verification_status for r in affected
                        },
                        previous_evidence_ids=tuple(
                            sorted(
                                {
                                    p
                                    for t in state.subquestions
                                    if (t.parent_task_id or t.task_id) == root
                                    for p in t.passage_ids
                                },
                                key=str,
                            )
                        ),
                    )
                )
            state = state.evolve(
                subquestions=(*state.subquestions, *tasks),
                retry_history=(*state.retry_history, *retries),
                active_retry_ids=tuple(r.retry_id for r in retries),
            )
            state = await self._store.save(
                state,
                agent="planner",
                event="research_retry_planned",
                details={
                    "retry_ids": [str(r.retry_id) for r in retries],
                    "task_ids": [str(t.task_id) for t in tasks],
                    "queries": [r.search_query for r in retries],
                },
            )
        except (WorkflowError, ModelUnavailable) as error:
            state = state.evolve(
                correction_stopped=True,
                correction_stop_reason=error.code,
                active_retry_ids=(),
                errors=(*state.errors, error.code),
            )
            state = await self._store.save(
                state,
                agent="planner",
                event="followup_planner_stopped",
                details={"error_code": error.code},
            )
        state = state.evolve(
            retry_duration_seconds=state.retry_duration_seconds + round(perf_counter() - started, 6)
        )
        state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
        return channels(await self._store.save(state, agent="graph", event="retry_plan_recorded"))

    async def research(self, state: WorkflowState):
        started = perf_counter()
        active = set(state.active_retry_ids)
        retries = tuple(
            r.model_copy(update={"status": "researching"}) if r.retry_id in active else r
            for r in state.retry_history
        )
        state = await self._store.save(
            state.evolve(execution_status="retrying", retry_history=retries),
            agent="researcher",
            event="retry_research_started",
            details={"retry_ids": [str(r) for r in state.active_retry_ids]},
        )
        task_ids = {r.task_id for r in retries if r.retry_id in active}
        await self._researcher.research(
            state.run_id, tuple(t for t in state.subquestions if t.task_id in task_ids)
        )
        state = await self._store.get(state.run_id)
        evidence = await self._store.evidence(state.run_id)
        passages = {p.passage_id: p for p in evidence.passages}
        retries = []
        for retry in state.retry_history:
            if retry.retry_id not in active:
                retries.append(retry)
                continue
            task = next(t for t in state.subquestions if t.task_id == retry.task_id)
            seen = {
                sha256(passages[p].text.encode()).hexdigest()
                for p in retry.previous_evidence_ids
                if p in passages
            }
            novel = []
            for passage_id in task.passage_ids:
                fingerprint = sha256(passages[passage_id].text.encode()).hexdigest()
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    novel.append(passage_id)
            if novel:
                status, error = "collected", None
            elif task.status in {"failed", "budget_exhausted"}:
                status, error = "failed", task.error_code
            else:
                status = "no_new_evidence"
                error = "duplicate_evidence" if task.passage_ids else "no_evidence_found"
            updated = retry.model_copy(
                update={
                    "status": status,
                    "error_code": error,
                    "new_evidence_ids": tuple(novel),
                    "finished_at": None if novel else utc_now(),
                    "duration_seconds": (utc_now() - retry.created_at).total_seconds(),
                }
            )
            retries.append(updated)
        elapsed = round(perf_counter() - started, 6)
        state = state.evolve(
            retry_history=tuple(retries),
            research_duration_seconds=state.research_duration_seconds + elapsed,
            retry_duration_seconds=state.retry_duration_seconds + elapsed,
            source_references=tuple(
                SourceReference(
                    source_id=s.source_id,
                    original_url=s.original_url,
                    title=s.title,
                    retrieved_at=s.retrieved_at,
                )
                for s in evidence.sources
            ),
            errors=tuple(
                sorted(
                    set(
                        (
                            *state.errors,
                            *(
                                r.error_code
                                for r in retries
                                if r.retry_id in active and r.status == "failed" and r.error_code
                            ),
                        )
                    )
                )
            ),
        )
        state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
        return channels(
            await self._store.save(
                state,
                agent="researcher",
                event="retry_research_completed",
                details={
                    "new_evidence_ids": [
                        str(p) for r in retries if r.retry_id in active for p in r.new_evidence_ids
                    ],
                    "statuses": [r.status for r in retries if r.retry_id in active],
                },
            )
        )

    async def reverify(self, state: WorkflowState):
        state = await self._store.save(
            state.evolve(execution_status="reverifying"),
            agent="verifier",
            event="reverification_started",
        )
        for retry_id in state.active_retry_ids:
            state = await self._store.get(state.run_id)
            retry = next(r for r in state.retry_history if r.retry_id == retry_id)
            if retry.status != "collected":
                continue
            started = perf_counter()
            results, drafted, error = (), (), None
            verification_elapsed, drafting_elapsed = 0, 0
            try:
                if retry.claim_ids:
                    await self._claims.attach_retry_evidence(
                        state.run_id, retry_id, retry.claim_ids, retry.new_evidence_ids
                    )
                else:
                    drafting_started = perf_counter()
                    task = next(t for t in state.subquestions if t.task_id == retry.task_id)
                    drafted = await self._drafter.draft(
                        state.run_id, state.original_question, (task,), batch_id=retry_id
                    )
                    drafting_elapsed = perf_counter() - drafting_started
                selected = {*retry.claim_ids, *(c.claim_id for c in drafted)}
                candidates = tuple(
                    c for c in await self._claims.claims(state.run_id) if c.claim_id in selected
                )
                if not candidates:
                    raise WorkflowError("no_candidate_claims_generated")
                verification_started = perf_counter()
                results = await self._verifier.verify_all(candidates, research_retry_id=retry_id)
                verification_elapsed = perf_counter() - verification_started
            except (WorkflowError, ModelUnavailable) as failure:
                error = failure.code
            state = await self._store.get(state.run_id)
            retry = retry.model_copy(
                update={
                    "status": "failed" if error else "completed",
                    "error_code": error,
                    "drafted_claim_ids": tuple(c.claim_id for c in drafted),
                    "updated_verdicts": {str(r.claim_id): r.verdict.value for r in results},
                    "finished_at": utc_now(),
                    "duration_seconds": (utc_now() - retry.created_at).total_seconds(),
                }
            )
            elapsed = round(perf_counter() - started, 6)

            def accumulated_tokens(field, state=state, results=results):
                previous = getattr(state, f"verification_{field}")
                previous = previous if state.verification_llm_requests else 0
                values = [getattr(r, field) for r in results if r.llm_requests]
                return (
                    previous + sum(values)
                    if previous is not None and all(value is not None for value in values)
                    else None
                )

            state = state.evolve(
                retry_history=tuple(
                    retry if r.retry_id == retry_id else r for r in state.retry_history
                ),
                retry_duration_seconds=state.retry_duration_seconds + elapsed,
                verification_duration_seconds=state.verification_duration_seconds
                + round(verification_elapsed, 6),
                drafting_duration_seconds=state.drafting_duration_seconds
                + round(drafting_elapsed, 6),
                verification_prompt_tokens=accumulated_tokens("prompt_tokens"),
                verification_completion_tokens=accumulated_tokens("completion_tokens"),
                verification_persistence_duration_ms=state.verification_persistence_duration_ms
                + sum(r.persistence_duration_ms for r in results),
                verification_llm_requests=state.verification_llm_requests
                + sum(r.llm_requests for r in results),
                verification_failures=state.verification_failures
                + sum(r.outcome != "completed" for r in results),
                errors=tuple(
                    sorted(
                        set(
                            (
                                *state.errors,
                                *((error,) if error else ()),
                                *(r.rejection_reason for r in results if r.rejection_reason),
                            )
                        )
                    )
                ),
                candidate_claim_ids=tuple(
                    c.claim_id for c in await self._claims.claims(state.run_id)
                ),
            )
            state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
            state = await self._store.save(
                state,
                agent="verifier",
                event="retry_reverified",
                details={
                    "retry_id": str(retry_id),
                    "new_evidence_ids": [str(p) for p in retry.new_evidence_ids],
                    "previous_verdicts": [f"{k}:{v}" for k, v in retry.previous_verdicts.items()],
                    "updated_verdicts": [f"{k}:{v}" for k, v in retry.updated_verdicts.items()],
                    "error_code": error or "",
                    "claims_evaluated": len(results),
                },
            )
        return channels(state)
