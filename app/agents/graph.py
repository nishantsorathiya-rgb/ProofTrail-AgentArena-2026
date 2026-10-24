from time import perf_counter
from uuid import uuid5

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from app.agents.correction import SelfCorrection
from app.agents.drafter import ClaimDrafter
from app.agents.planner import PlannerAgent
from app.agents.researcher import ResearcherAgent
from app.agents.verifier import VerifierAgent
from app.application.reports import ReportBuilder
from app.domain.claim_ports import ClaimStore
from app.domain.llm import ModelUnavailable
from app.domain.workflow import ResearchTask, SourceReference, WorkflowError, WorkflowState, utc_now
from app.domain.workflow_ports import WorkflowStore


def channels(state: WorkflowState) -> dict:
    return {name: getattr(state, name) for name in type(state).model_fields}


class ResearchWorkflow:
    def __init__(
        self,
        planner: PlannerAgent,
        researcher: ResearcherAgent,
        store: WorkflowStore,
        drafter: ClaimDrafter,
        verifier: VerifierAgent,
        claims: ClaimStore,
        correction: SelfCorrection,
        reports: ReportBuilder,
    ):
        self._planner, self._researcher, self._store = planner, researcher, store
        self._drafter, self._verifier, self._claims = drafter, verifier, claims
        self._correction, self._reports = correction, reports
        builder = StateGraph(WorkflowState)
        builder.add_node("plan", self._plan)
        builder.add_node("research", self._research)
        builder.add_node("collection_summary", self._summary)
        builder.add_node("draft", self._draft)
        builder.add_node("verify", self._verify)
        builder.add_node("verification_summary", self._verification_summary)
        builder.add_node("verification_decision", correction.decide)
        builder.add_node("plan_retry", correction.plan)
        builder.add_node("retry_research", correction.research)
        builder.add_node("reverify", correction.reverify)
        builder.add_node("synthesize", self._synthesize)
        builder.add_node("validate_report", self._validate_report)
        builder.add_edge(START, "plan")
        builder.add_conditional_edges(
            "plan", self._after_plan, {"research": "research", "stop": "collection_summary"}
        )
        builder.add_edge("research", "collection_summary")
        builder.add_conditional_edges(
            "collection_summary",
            self._after_collection,
            {
                "draft": "draft",
                "summary": "verification_summary",
                "legacy": END,
            },
        )
        builder.add_conditional_edges(
            "draft",
            self._after_draft,
            {
                "verify": "verify",
                "summary": "verification_summary",
            },
        )
        builder.add_edge("verify", "verification_summary")
        builder.add_conditional_edges(
            "verification_summary",
            self._after_summary,
            {"decision": "verification_decision", "legacy": END},
        )
        builder.add_conditional_edges(
            "verification_decision",
            self._after_decision,
            {"retry": "plan_retry", "report": "synthesize"},
        )
        builder.add_conditional_edges(
            "plan_retry",
            self._after_retry_plan,
            {"research": "retry_research", "decision": "verification_decision"},
        )
        builder.add_edge("retry_research", "reverify")
        builder.add_edge("reverify", "verification_summary")
        builder.add_edge("synthesize", "validate_report")
        builder.add_edge("validate_report", END)
        self.graph = builder.compile()

    @staticmethod
    def _after_plan(state: WorkflowState) -> str:
        return "stop" if state.execution_status == "failed" else "research"

    @staticmethod
    def _after_collection(state):
        if state.phase == "P3":
            return "legacy"
        return "draft" if state.retrieved_evidence_ids and state.status != "failed" else "summary"

    @staticmethod
    def _after_draft(state):
        return "verify" if state.candidate_claim_ids and state.status != "failed" else "summary"

    @staticmethod
    def _after_summary(state):
        return "decision" if state.phase == "P5" else "legacy"

    @staticmethod
    def _after_decision(state):
        return "retry" if state.eligible_gap_ids and state.status != "failed" else "report"

    @staticmethod
    def _after_retry_plan(state):
        return "research" if state.active_retry_ids else "decision"

    async def run(self, state: WorkflowState) -> WorkflowState:
        persisted = await self._store.get(state.run_id)
        if persisted is None or persisted.status != "queued":
            raise WorkflowError("run_not_queued")
        started = perf_counter()
        # Override tracing environment preferences: questions/evidence must stay local.
        with tracing_context(enabled=False):
            result = await self.graph.ainvoke(
                channels(state),
                config={
                    "callbacks": [],
                    "recursion_limit": 20 + 4 * state.budgets.max_research_tasks,
                },
            )
        final = WorkflowState.model_validate(result).evolve(
            total_duration_seconds=round(perf_counter() - started, 6),
            finished_at=utc_now(),
        )
        return await self._store.save(
            final,
            agent="graph",
            event="workflow_finished",
            details={
                "execution_status": final.execution_status,
                "total_duration_seconds": final.total_duration_seconds,
            },
        )

    async def _plan(self, state: WorkflowState) -> dict:
        started = perf_counter()
        state = await self._store.save(
            state.evolve(status="running", execution_status="planning", started_at=utc_now()),
            agent="planner",
            event="planner_started",
        )
        try:
            outcome = await self._planner.plan(state.run_id, state.original_question)
            tasks = tuple(
                ResearchTask(task_id=uuid5(state.run_id, f"task:{i}"), assignment=assignment)
                for i, assignment in enumerate(outcome.plan.subquestions)
            )
            warnings = (
                (*state.warnings, "duplicate_tasks_removed")
                if outcome.duplicates_removed
                else state.warnings
            )
            state = state.evolve(
                research_plan=outcome.plan,
                subquestions=tasks,
                warnings=warnings,
                planning_duration_seconds=round(perf_counter() - started, 6),
            )
            state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
            state = await self._store.save(
                state,
                agent="planner",
                event="planner_completed",
                details={
                    "task_count": len(tasks),
                    "duplicates_removed": outcome.duplicates_removed,
                },
            )
        except (WorkflowError, ModelUnavailable) as error:
            state = state.evolve(
                status="failed",
                execution_status="failed",
                errors=(*state.errors, error.code),
                planning_duration_seconds=round(perf_counter() - started, 6),
            )
            state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
            state = await self._store.save(
                state, agent="planner", event="planner_failed", details={"error_code": error.code}
            )
        return channels(state)

    async def _research(self, state: WorkflowState) -> dict:
        started = perf_counter()
        state = await self._store.save(
            state.evolve(execution_status="researching"),
            agent="researcher",
            event="research_started",
            details={"task_count": len(state.subquestions)},
        )
        await self._researcher.research(state.run_id, state.subquestions)
        state = await self._store.get(state.run_id)
        state = state.evolve(research_duration_seconds=round(perf_counter() - started, 6))
        state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
        state = await self._store.save(state, agent="researcher", event="research_completed")
        return channels(state)

    async def _summary(self, state: WorkflowState) -> dict:
        evidence = await self._store.evidence(state.run_id)
        if state.execution_status != "failed":
            statuses = {task.status for task in state.subquestions}
            if evidence.sources:
                outcome = "evidence_collected" if statuses == {"collected"} else "partial"
            elif "budget_exhausted" in statuses:
                outcome = "budget_exhausted"
            elif statuses == {"empty"}:
                outcome = "no_evidence"
            else:
                outcome = "failed"
            warnings = tuple(
                sorted(
                    set(
                        (
                            *state.warnings,
                            *(code for task in state.subquestions for code in task.warnings),
                        )
                    )
                )
            )
            errors = tuple(
                sorted(
                    set(
                        (
                            *state.errors,
                            *(task.error_code for task in state.subquestions if task.error_code),
                        )
                    )
                )
            )
            state = state.evolve(
                status="failed" if outcome in {"failed", "budget_exhausted"} else "completed",
                execution_status=outcome,
                warnings=warnings,
                errors=errors,
            )
        state = state.evolve(
            retrieved_evidence_ids=tuple(p.passage_id for p in evidence.passages),
            source_references=tuple(
                SourceReference(
                    source_id=s.source_id,
                    original_url=s.original_url,
                    title=s.title,
                    retrieved_at=s.retrieved_at,
                )
                for s in evidence.sources
            ),
            collection_status=state.execution_status,
            status="running"
            if (state.phase == "P4" and evidence.sources and state.status != "failed")
            or (state.phase == "P5" and state.research_plan is not None)
            else state.status,
            finished_at=None if state.phase in {"P4", "P5"} else utc_now(),
        )
        state = await self._store.save(
            state,
            agent="graph",
            event="collection_summarized",
            details={
                "source_count": len(evidence.sources),
                "passage_count": len(evidence.passages),
                "verification_status": state.verification_status,
            },
        )
        return channels(state)

    async def _draft(self, state: WorkflowState) -> dict:
        started = perf_counter()
        state = await self._store.save(
            state.evolve(execution_status="drafting"),
            agent="drafter",
            event="draft_started",
        )
        try:
            candidates = await self._drafter.draft(
                state.run_id,
                state.original_question,
                state.subquestions,
            )
            state = state.evolve(candidate_claim_ids=tuple(c.claim_id for c in candidates))
            event, details = "draft_completed", {"claim_count": len(candidates)}
        except (WorkflowError, ModelUnavailable) as error:
            state = state.evolve(
                status="failed",
                verification_status="failed",
                execution_status="budget_exhausted"
                if error.code == "model_budget_exhausted"
                else "failed",
                errors=(*state.errors, error.code),
            )
            event, details = "draft_failed", {"error_code": error.code}
        state = state.evolve(drafting_duration_seconds=round(perf_counter() - started, 6))
        state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
        return channels(
            await self._store.save(state, agent="drafter", event=event, details=details)
        )

    async def _verify(self, state: WorkflowState) -> dict:
        started = perf_counter()
        state = await self._store.save(
            state.evolve(execution_status="verifying", verification_status="running"),
            agent="verifier",
            event="verification_started",
            details={"claim_count": len(state.candidate_claim_ids)},
        )
        candidates = await self._claims.claims(state.run_id)
        results = await self._verifier.verify_all(candidates)
        state = await self._store.get(state.run_id)
        failures = [r for r in results if r.outcome != "completed"]

        def tokens(field):
            values = [getattr(r, field) for r in results if r.llm_requests]
            return sum(values) if values and all(v is not None for v in values) else None

        state = state.evolve(
            verification_status="partial" if failures else "completed",
            verification_duration_seconds=round(perf_counter() - started, 6),
            verification_persistence_duration_ms=round(
                sum(r.persistence_duration_ms for r in results), 3
            ),
            verification_llm_requests=sum(r.llm_requests for r in results),
            verification_failures=len(failures),
            verification_prompt_tokens=tokens("prompt_tokens"),
            verification_completion_tokens=tokens("completion_tokens"),
            errors=tuple(
                sorted(
                    set(
                        (
                            *state.errors,
                            *(r.rejection_reason for r in failures if r.rejection_reason),
                        )
                    )
                )
            ),
        )
        return channels(
            await self._store.save(
                state,
                agent="verifier",
                event="verification_completed",
                details={"claims_processed": len(results), "verification_failures": len(failures)},
            )
        )

    async def _verification_summary(self, state: WorkflowState) -> dict:
        collection = await self._claims.collection(state.run_id)
        counts = {}
        for record in collection.claims:
            counts[record.verification_status] = counts.get(record.verification_status, 0) + 1
        if state.verification_status in {"pending", "not_implemented"}:
            state = state.evolve(verification_status="not_run")
            if state.execution_status == "drafting" and not state.candidate_claim_ids:
                state = state.evolve(warnings=(*state.warnings, "no_candidate_claims_generated"))
        if state.phase == "P5" and collection.claims:
            state = state.evolve(
                verification_status="partial"
                if any(
                    r.current_integrity_error
                    or not r.attempts
                    or r.attempts[-1].result is None
                    or r.attempts[-1].result.outcome != "completed"
                    for r in collection.claims
                )
                else "completed"
            )
        if state.status == "running" and state.phase != "P5":
            state = state.evolve(
                status="completed",
                execution_status="verification_completed"
                if state.verification_status == "completed"
                and state.collection_status == "evidence_collected"
                else "partial",
            )
        state = state.evolve(
            verification_counts=counts,
            supported_claim_ids=collection.supported_claim_ids,
            finished_at=None if state.phase == "P5" else utc_now(),
        )

        state = state.evolve(**await self._store.metrics(state.run_id, state.budgets))
        return channels(
            await self._store.save(
                state,
                agent="graph",
                event="verification_summarized",
                details={
                    "claims_processed": len(collection.claims),
                    "supported_claim_count": len(collection.supported_claim_ids),
                    "verification_status": state.verification_status,
                },
            )
        )

    async def _synthesize(self, state):
        return channels(await self._reports.synthesize(state))

    async def _validate_report(self, state):
        return channels(await self._reports.validate_report(state))
