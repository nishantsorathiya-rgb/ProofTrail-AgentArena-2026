from uuid import UUID

from app.domain.ports import RunRepository
from app.domain.runs import ResearchRun


class ResearchRunService:
    """Create and retrieve persisted research runs without starting a workflow."""

    def __init__(self, repository: RunRepository) -> None:
        self._repository = repository

    async def create(self, question: str) -> ResearchRun:
        run = ResearchRun(question=question.strip())
        await self._repository.add(run)
        return run

    async def get(self, run_id: UUID) -> ResearchRun | None:
        return await self._repository.get(run_id)
