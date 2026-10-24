import json
import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter

from app.infrastructure.database import Database
from app.infrastructure.observability import JsonFormatter
from app.main import create_app


@asynccontextmanager
async def api_client(application):
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://offline-test",
        ) as client:
            yield client


async def test_health_is_local_ready_and_preserves_request_id(settings):
    request_id = str(uuid4())
    async with api_client(create_app(settings)) as client:
        response = await client.get("/health", headers={"X-Request-ID": request_id})
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "phase": "P1", "database": "sqlite"}
        assert response.headers["x-request-id"] == request_id
        assert response.json().get("api_key") is None


async def test_unknown_routes_use_safe_errors_and_replace_invalid_request_id(settings):
    async with api_client(create_app(settings)) as client:
        response = await client.get(
            "/unknown/absent?key=PRIVATE",
            headers={"X-Request-ID": "bad PRIVATE"},
        )
        assert response.status_code == 404
        request_id = response.headers["x-request-id"]
        UUID(request_id)
        assert response.json() == {"error": {"code": "not_found", "request_id": request_id}}
        assert "PRIVATE" not in response.text
        assert (await client.post("/unknown", json={"question": "Private"})).status_code == 404
        assert (await client.post("/health")).status_code == 405


async def test_unexpected_error_has_request_id_and_logs_no_private_text(settings):
    application = create_app(settings)
    router = APIRouter()

    @router.get("/test-failure")
    async def fail():
        raise RuntimeError("PRIVATE question / fixture-key-do-not-log")

    application.include_router(router)
    output = []

    class CollectLogs(logging.Handler):
        def emit(self, record):
            output.append(JsonFormatter().format(record))

    logger = logging.getLogger("prooftrail")
    handler = CollectLogs()
    logger.addHandler(handler)
    try:
        async with api_client(application) as client:
            response = await client.get("/test-failure?secret=PRIVATE")
            assert response.status_code == 500
            assert response.json() == {
                "error": {
                    "code": "internal_error",
                    "request_id": response.headers["x-request-id"],
                }
            }
        logs = "\n".join(output)
        assert "PRIVATE" not in logs
        assert "fixture-key" not in logs
        assert "secret=" not in logs
        entries = [json.loads(line) for line in output]
        assert any(entry.get("error_type") == "RuntimeError" for entry in entries)
        assert any(entry.get("status_code") == 500 for entry in entries)
    finally:
        logger.removeHandler(handler)


async def test_failed_database_probe_returns_503(settings, monkeypatch):
    async def unavailable(self):
        return False

    monkeypatch.setattr(Database, "is_available", unavailable)
    async with api_client(create_app(settings)) as client:
        response = await client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"


async def test_validation_error_does_not_echo_private_input(settings):
    application = create_app(settings)

    @application.get("/test-validation")
    async def validate(number: int):
        return {"number": number}

    async with api_client(application) as client:
        response = await client.get("/test-validation?number=PRIVATE-KEY")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
        assert "PRIVATE" not in response.text
