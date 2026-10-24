from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            error.status_code,
            "request_failed",
        )
        return JSONResponse(
            {
                "error": {"code": code, "request_id": request.state.request_id},
            },
            status_code=error.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse(
            {
                "error": {"code": "invalid_request", "request_id": request.state.request_id},
            },
            status_code=422,
        )
