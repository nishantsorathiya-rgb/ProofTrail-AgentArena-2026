from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.application.health import HealthService


class HealthResponse(BaseModel):
    status: str
    phase: str = "P1"
    database: str = "sqlite"


def health_router(service: HealthService) -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    async def health():
        ready = await service.is_ready()
        response = HealthResponse(status="ok" if ready else "degraded")
        return JSONResponse(response.model_dump(), status_code=200 if ready else 503)

    return router
