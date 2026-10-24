from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.application.coordinator import LocalRunCoordinator
from app.application.reports import markdown_report
from app.domain.claims import ClaimCollection
from app.domain.workflow import CollectedEvidence, WorkflowError, WorkflowState


class StartResearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("A nonblank research question is required.")
        return value.strip()


class AcceptedRun(BaseModel):
    run_id: UUID
    status: str = "queued"
    phase: str = "P5"
    verification_status: str = "pending"


def research_router(coordinator: LocalRunCoordinator) -> APIRouter:
    router = APIRouter()

    @router.post("/research", status_code=202, response_model=AcceptedRun)
    async def start(body: StartResearch):
        try:
            state = await coordinator.submit(body.question)
        except WorkflowError:
            raise HTTPException(status_code=503) from None
        return AcceptedRun(
            run_id=state.run_id, phase=state.phase, verification_status=state.verification_status
        )

    @router.get(
        "/research/{run_id}",
        response_model=WorkflowState,
        response_model_exclude={"pending_report_json"},
    )
    async def status(run_id: UUID):
        try:
            return await coordinator.get(run_id)
        except WorkflowError:
            raise HTTPException(status_code=404) from None

    @router.get("/research/{run_id}/evidence", response_model=CollectedEvidence)
    async def evidence(run_id: UUID):
        try:
            return await coordinator.evidence(run_id)
        except WorkflowError as error:
            raise HTTPException(
                status_code=404 if error.code == "research_run_not_found" else 500
            ) from None

    @router.get("/research/{run_id}/claims", response_model=ClaimCollection)
    async def claims(run_id: UUID):
        try:
            return await coordinator.claims(run_id)
        except WorkflowError as error:
            raise HTTPException(
                status_code=404 if error.code == "research_run_not_found" else 500
            ) from None

    @router.get("/research/{run_id}/audit")
    async def audit(run_id: UUID):
        try:
            state = await coordinator.get(run_id)
        except WorkflowError:
            raise HTTPException(status_code=404) from None
        return {"run_id": state.run_id, "events": state.audit_events}

    @router.get("/research/{run_id}/retries")
    async def retries(run_id: UUID):
        try:
            state = await coordinator.get(run_id)
        except WorkflowError:
            raise HTTPException(status_code=404) from None
        return {
            "run_id": state.run_id,
            "retry_counts": state.retry_counts,
            "retries": state.retry_history,
        }

    @router.get("/research/{run_id}/report")
    async def report(run_id: UUID, format: Literal["json", "markdown"] = "json"):
        try:
            state = await coordinator.get(run_id)
            result = await coordinator.report(run_id)
        except WorkflowError as error:
            if error.code == "research_run_not_found":
                raise HTTPException(status_code=404) from None
            return JSONResponse(
                {"run_id": str(run_id), "status": "unavailable", "error_code": error.code},
                status_code=409,
            )
        if result is None:
            failed = state.status in {"failed", "interrupted"}
            return JSONResponse(
                {
                    "run_id": str(run_id),
                    "status": "failed" if failed else "pending",
                    "report": None,
                },
                status_code=409 if failed else 202,
            )
        if format == "markdown":
            return Response(markdown_report(result), media_type="text/plain")
        return result

    return router
