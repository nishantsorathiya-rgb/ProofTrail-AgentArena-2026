import asyncio
import logging
from uuid import UUID, uuid4

from app.domain.claim_ports import ClaimStore
from app.domain.reports import ReportStore
from app.domain.workflow import BudgetState, WorkflowError, WorkflowState
from app.domain.workflow_ports import WorkflowPort, WorkflowStore

logger = logging.getLogger("prooftrail")


class LocalRunCoordinator:
    """Bounded single-process local jobs; persisted state is not a durable distributed queue."""

    def __init__(
        self,
        workflow: WorkflowPort,
        store: WorkflowStore,
        limits: BudgetState,
        *,
        mode: str,
        active_runs: int = 1,
        pending_runs: int = 4,
        timeout_seconds: float = 120,
        phase: str = "P5",
        claims: ClaimStore | None = None,
        reports: ReportStore | None = None,
    ):
        self._workflow, self._store, self._limits, self._mode = workflow, store, limits, mode
        self._capacity = active_runs + pending_runs
        self._semaphore = asyncio.Semaphore(active_runs)
        self._admission = asyncio.Lock()
        self._jobs: dict[UUID, asyncio.Task] = {}
        self._timeout = timeout_seconds
        self._closing = False
        self._phase = phase
        self._claims = claims
        self._reports = reports

    async def start(self) -> None:
        await self._store.interrupt_unfinished()

    async def submit(self, question: str) -> WorkflowState:
        async with self._admission:
            if self._closing or len(self._jobs) >= self._capacity:
                raise WorkflowError("coordinator_capacity_exhausted")
            state = WorkflowState(
                run_id=uuid4(),
                original_question=question.strip(),
                budgets=self._limits,
                mode=self._mode,
                phase=self._phase,
                verification_status="pending" if self._phase in {"P4", "P5"} else "not_implemented",
                report_generation_status="pending" if self._phase == "P5" else "not_implemented",
            )
            state = await self._store.create(state)
            self._jobs[state.run_id] = asyncio.create_task(self._execute(state))
            return state

    async def _execute(self, state: WorkflowState) -> None:
        try:
            async with self._semaphore:
                try:
                    await asyncio.wait_for(self._workflow.run(state), timeout=self._timeout)
                except TimeoutError:
                    await self._store.stop(state.run_id, "workflow_timeout")
        except asyncio.CancelledError:
            await self._store.stop(state.run_id, "coordinator_shutdown", interrupted=True)
        except Exception:
            try:
                await self._store.stop(state.run_id, "workflow_internal_error")
            except Exception as error:
                logger.error("workflow_storage_failed", extra={"error_type": type(error).__name__})
        finally:
            self._jobs.pop(state.run_id, None)

    async def get(self, run_id: UUID) -> WorkflowState:
        state = await self._store.get(run_id)
        if state is None:
            raise WorkflowError("research_run_not_found")
        state = state.evolve(**await self._store.metrics(run_id, state.budgets))
        if self._claims and state.phase in {"P4", "P5"} and state.candidate_claim_ids:
            collection = await self._claims.collection(run_id)
            counts = {}
            for record in collection.claims:
                counts[record.verification_status] = counts.get(record.verification_status, 0) + 1
            state = state.evolve(
                supported_claim_ids=collection.supported_claim_ids,
                verification_counts=counts,
            )
        return state

    async def claims(self, run_id: UUID):
        await self.get(run_id)
        if self._claims is None:
            raise WorkflowError("claims_unavailable")
        return await self._claims.collection(run_id)

    async def evidence(self, run_id: UUID):
        await self.get(run_id)
        return await self._store.evidence(run_id)

    async def report(self, run_id: UUID):
        state = await self.get(run_id)
        if state.phase != "P5" or self._reports is None:
            raise WorkflowError("reports_not_implemented_for_phase")
        return await self._reports.get(run_id)

    async def wait_idle(self) -> None:
        jobs = list(self._jobs.values())
        if jobs:
            await asyncio.gather(*jobs)

    async def aclose(self) -> None:
        self._closing = True
        run_ids = list(self._jobs)
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        # A task cancelled before its coroutine starts cannot execute its own cleanup.
        for run_id in run_ids:
            state = await self._store.get(run_id)
            if state and state.status in {"queued", "running"}:
                await self._store.stop(run_id, "coordinator_shutdown", interrupted=True)
            self._jobs.pop(run_id, None)
