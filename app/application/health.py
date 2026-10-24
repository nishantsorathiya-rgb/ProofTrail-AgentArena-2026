from dataclasses import dataclass

from app.domain.ports import StorageHealthPort


@dataclass(frozen=True, slots=True)
class HealthService:
    storage: StorageHealthPort

    async def is_ready(self) -> bool:
        return await self.storage.is_available()
