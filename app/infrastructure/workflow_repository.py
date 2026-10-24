import json
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.sqlite import insert

from app.domain.llm import LLMResult
from app.domain.workflow import (
    AuditEvent,
    BudgetState,
    CollectedEvidence,
    ResearchTask,
    WorkflowError,
    WorkflowState,
    task_key,
    utc_now,
)
from app.infrastructure.claim_models import ClaimRow, VerificationRow
from app.infrastructure.database import Database, ResearchRunRow
from app.infrastructure.evidence_models import ExtractionRow, UsageRow
from app.infrastructure.evidence_repository import SQLiteEvidenceRepository
from app.infrastructure.report_models import RetryRow
from app.infrastructure.workflow_models import (
    AuditRow,
    ModelBudgetRow,
    ModelUsageRow,
    SubquestionRow,
    WorkflowRow,
)


class SQLiteWorkflowRepository:
    def __init__(self, database: Database):
        self._sessions = database.sessions
        self._evidence = SQLiteEvidenceRepository(database)

    async def create(self, state: WorkflowState) -> WorkflowState:
        async with self._sessions.begin() as session:
            session.add(
                ResearchRunRow(
                    run_id=str(state.run_id),
                    question=state.original_question,
                    status="queued",
                    created_at=state.created_at,
                    updated_at=state.updated_at,
                )
            )
            await session.flush()
            session.add(
                WorkflowRow(
                    run_id=str(state.run_id),
                    state_json=state.model_dump_json(),
                    updated_at=state.updated_at,
                )
            )
            await session.flush()
            return await self._write(session, state, agent="coordinator", event="run_queued")

    async def get(self, run_id: UUID) -> WorkflowState | None:
        async with self._sessions() as session:
            row = await session.get(WorkflowRow, str(run_id))
            return WorkflowState.model_validate_json(row.state_json) if row else None

    async def _write(
        self,
        session,
        state: WorkflowState,
        *,
        agent: str,
        event: str,
        details: dict | None = None,
        task_id: UUID | None = None,
    ) -> WorkflowState:
        row = await session.get(WorkflowRow, str(state.run_id))
        current = WorkflowState.model_validate_json(row.state_json)
        if event == "planner_started" and current.execution_status != "queued":
            raise WorkflowError("run_not_queued")
        if state.phase == "P5":
            if len(state.subquestions) > state.budgets.max_research_tasks:
                raise WorkflowError("research_task_budget_exhausted")
            known = {r.retry_id: r for r in current.retry_history}
            seen_queries = {task_key(t.assignment.search_query) for t in current.subquestions}
            for retry in state.retry_history:
                old = known.get(retry.retry_id)
                if old:
                    immutable = (
                        "root_task_id",
                        "task_id",
                        "number",
                        "trigger",
                        "question",
                        "search_query",
                        "claim_ids",
                        "previous_verdicts",
                        "previous_evidence_ids",
                        "created_at",
                    )
                    if any(getattr(old, key) != getattr(retry, key) for key in immutable):
                        raise WorkflowError("retry_history_rewrite_rejected")
                    continue
                prior = [r for r in current.retry_history if r.root_task_id == retry.root_task_id]
                if (
                    retry.number != len(prior) + 1
                    or retry.number > state.budgets.max_retries_per_subquestion
                    or task_key(retry.search_query) in seen_queries
                ):
                    raise WorkflowError("retry_limit_or_duplicate_query")
                child = next((t for t in state.subquestions if t.task_id == retry.task_id), None)
                root = next(
                    (t for t in state.subquestions if t.task_id == retry.root_task_id), None
                )
                if (
                    not child
                    or not root
                    or root.parent_task_id is not None
                    or child.parent_task_id != root.task_id
                    or child.retry_number != retry.number
                    or child.assignment.search_query != retry.search_query
                ):
                    raise WorkflowError("invalid_retry_assignment")
                seen_queries.add(task_key(retry.search_query))
        audit = AuditEvent(
            sequence=len(current.audit_events) + 1,
            agent=agent,
            event=event,
            task_id=task_id,
            details=details or {},
        )
        state = state.evolve(
            updated_at=utc_now(),
            audit_events=(*current.audit_events, audit),
            research_task_status={str(t.task_id): t.status for t in state.subquestions},
            retry_counts={
                str(t.task_id): sum(r.root_task_id == t.task_id for r in state.retry_history)
                for t in state.subquestions
                if t.parent_task_id is None
            },
        )
        row.state_json, row.updated_at = state.model_dump_json(), state.updated_at
        run = await session.get(ResearchRunRow, str(state.run_id))
        run.status, run.updated_at = state.status, state.updated_at
        session.add(
            AuditRow(
                event_id=str(audit.event_id),
                run_id=str(state.run_id),
                sequence=audit.sequence,
                timestamp=audit.timestamp,
                agent=agent,
                event=event,
                task_id=str(task_id) if task_id else None,
                details_json=json.dumps(audit.details),
            )
        )
        for task in state.subquestions:
            values = dict(
                task_id=str(task.task_id),
                run_id=str(state.run_id),
                question=task.assignment.question,
                search_query=task.assignment.search_query,
                evidence_requirement=task.assignment.evidence_requirement,
                status=task.status,
                started_at=task.started_at,
                finished_at=task.finished_at,
                duration_seconds=task.duration_seconds,
                error_code=task.error_code,
                source_ids_json=json.dumps([str(v) for v in task.source_ids]),
                passage_ids_json=json.dumps([str(v) for v in task.passage_ids]),
                parent_task_id=str(task.parent_task_id) if task.parent_task_id else None,
                retry_number=task.retry_number,
            )
            statement = insert(SubquestionRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["task_id"],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in {"task_id", "run_id"}
                    },
                )
            )
        for retry in state.retry_history:
            statement = insert(RetryRow).values(
                retry_id=str(retry.retry_id),
                run_id=str(state.run_id),
                root_task_id=str(retry.root_task_id),
                task_id=str(retry.task_id),
                number=retry.number,
                status=retry.status,
                record_json=retry.model_dump_json(),
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["retry_id"],
                    set_={"status": retry.status, "record_json": retry.model_dump_json()},
                )
            )
        return state

    async def save(
        self, state: WorkflowState, *, agent: str, event: str, details: dict | None = None
    ) -> WorkflowState:
        async with self._sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            return await self._write(session, state, agent=agent, event=event, details=details)

    async def task_update(self, run_id: UUID, task: ResearchTask, *, event: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(WorkflowRow, str(run_id))
            state = WorkflowState.model_validate_json(row.state_json)
            if state.status != "running" or not any(
                t.task_id == task.task_id for t in state.subquestions
            ):
                raise WorkflowError("invalid_task_transition")
            old = next(item for item in state.subquestions if item.task_id == task.task_id)
            starting = (
                event == "task_started" and old.status == "queued" and task.status == "running"
            )
            finishing = (
                event == "task_finished"
                and old.status == "running"
                and task.status in {"collected", "partial", "empty", "failed", "budget_exhausted"}
            )
            if (
                not (starting or finishing)
                or old.assignment != task.assignment
                or old.parent_task_id != task.parent_task_id
                or old.retry_number != task.retry_number
            ):
                raise WorkflowError("invalid_task_transition")
            tasks = tuple(
                task if old.task_id == task.task_id else old for old in state.subquestions
            )
            evidence_ids = tuple(sorted({p for item in tasks for p in item.passage_ids}, key=str))
            await self._write(
                session,
                state.evolve(subquestions=tasks, retrieved_evidence_ids=evidence_ids),
                agent="researcher",
                event=event,
                task_id=task.task_id,
                details={
                    "status": task.status,
                    "source_ids": [str(v) for v in task.source_ids],
                    "passage_ids": [str(v) for v in task.passage_ids],
                    "duration_seconds": task.duration_seconds,
                    "error_code": task.error_code or "",
                },
            )

    async def evidence(self, run_id: UUID) -> CollectedEvidence:
        sources = await self._evidence.sources_for_run(run_id)
        passages = []
        for source in sources:
            stored = await self._evidence.passages_for_source(source.source_id)
            if any(source.content[p.start_char : p.end_char] != p.text for p in stored):
                raise WorkflowError("evidence_integrity_failed")
            passages.extend(stored)
        state = await self.get(run_id)
        return CollectedEvidence(
            run_id=run_id,
            sources=sources,
            passages=tuple(passages),
            verification_status="unverified_source_material"
            if state and state.phase in {"P4", "P5"}
            else "not_implemented",
        )

    async def metrics(self, run_id: UUID, limits: BudgetState) -> dict:
        async with self._sessions() as session:
            usages = (
                await session.scalars(select(UsageRow).where(UsageRow.run_id == str(run_id)))
            ).all()
            models = (
                await session.scalars(
                    select(ModelUsageRow).where(ModelUsageRow.run_id == str(run_id))
                )
            ).all()
            extracts = (
                await session.scalars(
                    select(ExtractionRow).where(ExtractionRow.run_id == str(run_id))
                )
            ).all()
        budget = limits.model_copy(
            update={
                "model_attempts": len(models),
                "llm_calls": sum(item.outbound_called for item in models),
                "search_requests": sum(item.operation == "search" for item in usages),
                "extract_requests": sum(item.operation == "extract" for item in usages),
                "fetch_units": sum(item.units for item in usages if item.operation == "extract"),
            }
        )

        def token_total(field):
            values = [getattr(item, field) for item in models if item.outbound_called]
            return sum(values) if values and all(value is not None for value in values) else None

        return {
            "budgets": budget,
            "cache_hits": sum(item.status == "cache_hit" for item in extracts),
            "prompt_tokens": token_total("prompt_tokens"),
            "completion_tokens": token_total("completion_tokens"),
        }

    async def reserve_model(self, run_id: UUID, max_calls: int) -> UUID:
        usage_id = uuid4()
        async with self._sessions.begin() as session:
            await session.execute(
                insert(ModelBudgetRow)
                .values(run_id=str(run_id), max_calls=max_calls, attempts=0)
                .on_conflict_do_nothing(index_elements=["run_id"])
            )
            result = await session.execute(
                update(ModelBudgetRow)
                .where(
                    ModelBudgetRow.run_id == str(run_id),
                    ModelBudgetRow.attempts < max_calls,
                    ModelBudgetRow.attempts < ModelBudgetRow.max_calls,
                )
                .values(attempts=ModelBudgetRow.attempts + 1)
            )
            if result.rowcount != 1:
                raise WorkflowError("model_budget_exhausted")
            session.add(
                ModelUsageRow(
                    usage_id=str(usage_id),
                    run_id=str(run_id),
                    started_at=utc_now(),
                    status="pending",
                    outbound_called=False,
                    elapsed_seconds=0,
                    error_code=None,
                    model=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                )
            )
        return usage_id

    async def finish_model(
        self,
        usage_id: UUID,
        *,
        result: LLMResult | None,
        error_code: str | None,
        elapsed_seconds: float,
        outbound_called: bool,
    ) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(ModelUsageRow, str(usage_id))
            row.status = "failed" if error_code else "success"
            row.error_code, row.outbound_called = error_code, outbound_called
            row.elapsed_seconds = round(elapsed_seconds, 6)
            if result:
                row.model, row.prompt_tokens = result.model[:128], result.prompt_tokens
                row.completion_tokens = result.completion_tokens

    async def stop(self, run_id: UUID, code: str, *, interrupted: bool = False) -> WorkflowState:
        state = await self.get(run_id)
        if state.status in {"completed", "failed", "interrupted"} and state.finished_at is not None:
            return state
        terminal = "interrupted" if interrupted else "failed"
        async with self._sessions.begin() as session:
            pending = (
                await session.scalars(
                    select(VerificationRow).where(
                        VerificationRow.run_id == str(run_id), VerificationRow.status == "pending"
                    )
                )
            ).all()
            for attempt in pending:
                attempt.status = "interrupted"
                claim = await session.get(ClaimRow, attempt.claim_id)
                claim.verification_status = "UNVERIFIED"
        tasks = tuple(
            task.model_copy(update={"status": "interrupted", "finished_at": utc_now()})
            if task.status in {"queued", "running"}
            else task
            for task in state.subquestions
        )
        state = state.evolve(
            status=terminal,
            execution_status=terminal,
            subquestions=tasks,
            finished_at=utc_now(),
            total_duration_seconds=max(
                state.total_duration_seconds,
                (utc_now() - state.started_at).total_seconds() if state.started_at else 0,
            ),
            errors=(*state.errors, code),
            verification_status="failed"
            if state.phase in {"P4", "P5"}
            else state.verification_status,
            report_generation_status="failed"
            if state.phase == "P5"
            else state.report_generation_status,
            pending_report_json=None,
            retry_history=tuple(
                r.model_copy(
                    update={
                        "status": "interrupted",
                        "finished_at": utc_now(),
                        "error_code": code,
                    }
                )
                if r.status in {"queued", "researching", "collected"}
                else r
                for r in state.retry_history
            ),
        )
        state = state.evolve(**await self.metrics(run_id, state.budgets))
        return await self.save(
            state, agent="coordinator", event=f"run_{terminal}", details={"error_code": code}
        )

    async def interrupt_unfinished(self) -> None:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(WorkflowRow)
                    .join(
                        ResearchRunRow,
                        ResearchRunRow.run_id == WorkflowRow.run_id,
                    )
                    .where(ResearchRunRow.status.in_(["queued", "running"]))
                )
            ).all()
        for row in rows:
            state = WorkflowState.model_validate_json(row.state_json)
            if state.status in {"queued", "running"}:
                await self.stop(state.run_id, "process_restarted", interrupted=True)
