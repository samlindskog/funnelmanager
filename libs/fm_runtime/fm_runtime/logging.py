"""Structured JSON logging to stdout — the only log destination.

Every record carries service, level, logger, message, and (when a request
context is active) the principal `sub`, request id, and traceparent. Extra
dict fields passed via `logger.info(..., extra={...})` are merged into the
JSON object. Uvicorn's own access log is silenced — the runtime middleware
emits a structured access line instead."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from fm_runtime.context import current_context

_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        ctx = current_context()
        if ctx is not None:
            if ctx.principal is not None:
                entry.setdefault("sub", ctx.principal.sub)
            if ctx.request_id:
                entry.setdefault("request_id", ctx.request_id)
            traceparent = ctx.trace_headers.get("traceparent")
            if traceparent:
                entry.setdefault("traceparent", traceparent)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # The runtime access log replaces uvicorn's (unstructured) one.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
