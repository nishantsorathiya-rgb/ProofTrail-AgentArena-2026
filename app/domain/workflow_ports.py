from typing import Protocol
from uuid import UUID

from app.domain.llm import LLMMessage, LLMResult
from app.domain.retrieval import CollectionResult
from app.domain.workflow import (
    BudgetState,
    CollectedEvidence,
    ResearchTask,
    WorkflowState,
)


class RunModelPort(Protocol):
    async def complete(
        self, run_id: UUID, messages: list[LLMMessage], *, max_tokens: int = 2048
    ) -> LLMResult: ...


class EvidenceCollectionPort(Protocol):
    async def collect(self, run_id: UUID, query: str) -> CollectionResult: ...


class WorkflowPort(Protocol):
    async def run(self, state: WorkflowState) -> WorkflowState: ...


class WorkflowStore(Protocol):
    async def create(self, state: WorkflowState) -> WorkflowState: ...

    async def get(self, run_id: UUID) -> WorkflowState | None: ...

    async def save(
        self, state: WorkflowState, *, agent: str, event: str, details: dict | None = None
    ) -> WorkflowState: ...

    async def task_update(self, run_id: UUID, task: ResearchTask, *, event: str) -> None: ...

    async def metrics(self, run_id: UUID, limits: BudgetState) -> dict: ...

    async def evidence(self, run_id: UUID) -> CollectedEvidence: ...

    async def reserve_model(self, run_id: UUID, max_calls: int) -> UUID: ...

    async def finish_model(
        self,
        usage_id: UUID,
        *,
        result: LLMResult | None,
        error_code: str | None,
        elapsed_seconds: float,
        outbound_called: bool,
    ) -> None: ...

    async def interrupt_unfinished(self) -> None: ...

    async def stop(
        self, run_id: UUID, code: str, *, interrupted: bool = False
    ) -> WorkflowState: ...
