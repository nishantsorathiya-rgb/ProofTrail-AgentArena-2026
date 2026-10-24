import asyncio
from dataclasses import replace
from time import perf_counter
from uuid import UUID

from app.domain.llm import LLMMessage, LLMPort, LLMResult, ModelUnavailable
from app.domain.workflow_ports import WorkflowStore


class BudgetedModel:
    """Persist a reservation before invoking the existing Mesh-only model port."""

    def __init__(self, model: LLMPort, store: WorkflowStore, max_calls: int):
        self._model, self._store, self._max_calls = model, store, max_calls

    async def complete(
        self, run_id: UUID, messages: list[LLMMessage], *, max_tokens: int = 2048
    ) -> LLMResult:
        usage_id = await self._store.reserve_model(run_id, self._max_calls)
        started = perf_counter()
        result = None
        code = None
        outbound = True
        try:
            result = await self._model.complete(messages, max_tokens=max_tokens)
            return replace(result, usage_id=usage_id)
        except ModelUnavailable as error:
            code = error.code
            outbound = code not in {
                "live_requests_disabled",
                "mesh_configuration_missing",
                "adapter_closed",
                "mesh_route_rejected",
            }
            raise
        except asyncio.CancelledError:
            code = "model_cancelled"
            raise
        except Exception:
            code = "model_internal_error"
            raise
        finally:
            await self._store.finish_model(
                usage_id,
                result=result,
                error_code=code,
                elapsed_seconds=perf_counter() - started,
                outbound_called=outbound,
            )
