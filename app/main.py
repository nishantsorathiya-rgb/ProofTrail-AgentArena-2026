import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from app.api.errors import register_error_handlers
from app.api.middleware import RequestLoggingMiddleware
from app.api.research import research_router
from app.api.routes import health_router
from app.application.config import Settings
from app.application.health import HealthService
from app.infrastructure.database import Database
from app.infrastructure.observability import configure_logging
from app.infrastructure.url_policy import Resolver
from app.workflow import WorkflowRuntime


def create_app(
    settings: Settings | None = None,
    *,
    mesh_transport: httpx.MockTransport | None = None,
    tavily_transport: httpx.MockTransport | None = None,
    resolver: Resolver | None = None,
    phase: str = "P5",
) -> FastAPI:
    """Composition root; importing the module performs no database/network I/O."""
    configure_logging()
    try:
        settings = settings or Settings()
    except ValidationError:
        raise RuntimeError("Invalid ProofTrail configuration; check local fields.") from None
    database = Database(settings.database_url)
    runtime = WorkflowRuntime(
        settings,
        database,
        mesh_transport=mesh_transport,
        tavily_transport=tavily_transport,
        resolver=resolver,
        phase=phase,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            await database.initialize()
            await runtime.coordinator.start()
        except Exception as error:
            await runtime.aclose()
            await database.aclose()
            logging.getLogger("prooftrail").error(
                "storage_startup_failed",
                extra={"error_type": type(error).__name__},
            )
            raise RuntimeError("ProofTrail storage startup failed; check configuration.") from None
        try:
            yield
        finally:
            await runtime.aclose()
            await database.aclose()

    application = FastAPI(
        title="ProofTrail",
        version="0.1.0",
        description="P5 bounded research, claim verification and evidence-grounded reports.",
        lifespan=lifespan,
    )
    application.add_middleware(RequestLoggingMiddleware)
    register_error_handlers(application)
    application.include_router(health_router(HealthService(database)))
    application.include_router(research_router(runtime.coordinator))
    application.state.coordinator = runtime.coordinator
    return application


app = create_app()
