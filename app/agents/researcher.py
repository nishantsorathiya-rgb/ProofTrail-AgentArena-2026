import asyncio
from time import perf_counter
from uuid import UUID

from app.domain.retrieval import BudgetExceeded, RetrievalError
from app.domain.workflow import ResearchTask, utc_now
from app.domain.workflow_ports import EvidenceCollectionPort, WorkflowStore


class ResearcherAgent:
    """Tool agent consuming assigned queries and collecting exact original-source evidence."""

    def __init__(
        self, collection: EvidenceCollectionPort, store: WorkflowStore, concurrency: int = 3
    ):
        self._collection, self._store = collection, store
        self._semaphore = asyncio.Semaphore(concurrency)

    async def research(self, run_id: UUID, tasks: tuple[ResearchTask, ...]) -> None:
        async with asyncio.TaskGroup() as group:
            for task in tasks:
                group.create_task(self._one(run_id, task))

    async def _one(self, run_id: UUID, task: ResearchTask) -> None:
        async with self._semaphore:
            started = perf_counter()
            running = task.model_copy(update={"status": "running", "started_at": utc_now()})
            await self._store.task_update(run_id, running, event="task_started")
            status, code, warnings, sources, passages = "failed", None, [], set(), set()
            try:
                result = await self._collection.collect(run_id, task.assignment.search_query)
                failures = []
                for item in result.evidence:
                    if item.source:
                        sources.add(item.source.source_id)
                        passages.update(p.passage_id for p in item.passages)
                    elif item.record.error_code:
                        failures.append(item.record.error_code)
                warnings = sorted(set(failures))
                if sources:
                    status = "partial" if failures else "collected"
                elif "retrieval_budget_exhausted" in failures:
                    status, code = "budget_exhausted", "retrieval_budget_exhausted"
                elif failures:
                    status, code = "failed", failures[0]
                else:
                    status = "empty"
                    warnings = ["no_evidence_found"]
            except BudgetExceeded as error:
                status, code = "budget_exhausted", error.code
            except RetrievalError as error:
                code = error.code
            except asyncio.CancelledError:
                raise
            except Exception:
                code = "research_task_internal_error"
            finished = running.model_copy(
                update={
                    "status": status,
                    "error_code": code,
                    "warnings": tuple(warnings),
                    "source_ids": tuple(sorted(sources, key=str)),
                    "passage_ids": tuple(sorted(passages, key=str)),
                    "finished_at": utc_now(),
                    "duration_seconds": round(perf_counter() - started, 6),
                }
            )
            await self._store.task_update(run_id, finished, event="task_finished")
