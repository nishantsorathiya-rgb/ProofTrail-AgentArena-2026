import json
import logging
from datetime import UTC, datetime

FIELDS = (
    "operation",
    "attempt_number",
    "error_code",
    "reported_credits",
    "request_id",
    "method",
    "route",
    "status_code",
    "duration_ms",
    "error_type",
    "prompt_tokens",
    "completion_tokens",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        payload.update({key: getattr(record, key) for key in FIELDS if hasattr(record, key)})
        # Deliberately exclude exception text, tracebacks, headers, query strings and payloads.
        return json.dumps(payload, ensure_ascii=True, allow_nan=False)


def configure_logging() -> None:
    logger = logging.getLogger("prooftrail")
    if not any(getattr(handler, "prooftrail_handler", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.prooftrail_handler = True
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # SDK debug logs may contain request/response bodies; never enable them in this application.
    for name in ("openai", "httpx", "httpcore"):
        provider_logger = logging.getLogger(name)
        provider_logger.setLevel(logging.CRITICAL + 1)
        provider_logger.disabled = True
        for existing_name in list(logging.Logger.manager.loggerDict):
            if existing_name.startswith(name + "."):
                child = logging.getLogger(existing_name)
                child.setLevel(logging.CRITICAL + 1)
                child.disabled = True
