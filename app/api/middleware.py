import logging
from time import perf_counter
from uuid import UUID, uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("prooftrail")


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        supplied_id = Headers(scope=scope).get("x-request-id", "")
        if len(supplied_id) == 36:
            try:
                request_id = str(UUID(supplied_id))
            except ValueError:
                pass
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status_code = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as error:
            logger.error(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "error_type": type(error).__name__,
                },
            )
            if response_started:
                raise
            response = JSONResponse(
                {
                    "error": {"code": "internal_error", "request_id": request_id},
                },
                status_code=500,
            )
            await response(scope, receive, send_with_id)
        finally:
            route = getattr(scope.get("route"), "path", "<unmatched>")
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "route": route,
                    "status_code": status_code,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                },
            )
