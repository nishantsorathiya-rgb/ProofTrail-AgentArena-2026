from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_id: UUID | None = None  # Set by the transactional budget port, never by model JSON.


class ModelUnavailable(Exception):
    """Sanitized model error, with no provider body, key, or prompt."""

    def __init__(self, code: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class LLMPort(Protocol):
    async def complete(self, messages: list[LLMMessage], *, max_tokens: int = 1024) -> LLMResult:
        """Complete through the configured runtime boundary."""
