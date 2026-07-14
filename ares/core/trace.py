"""Live activity trace — a rotating JSONL record of what the agent does.

One JSON object per line: inbound events (including the person's own messages),
the model's replies and any reasoning it exposes, each tool call and its result,
and the final delivered reply. Written through a RotatingFileHandler so the file
is size-capped and rolled over with a fixed number of retained backups; the
dashboard tails the active file for a live view.

Tracing must never break the agent: creating the tracer and every `emit` are
best-effort and swallow their own errors. When disabled (or if the file can't be
opened) the tracer is a silent no-op.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ares.core.utils.logging import get_logger

log = get_logger(__name__)

# Truncate very large string fields so a single record (e.g. a big tool result)
# can't dominate the file or a dashboard poll.
_MAX_FIELD = 8192


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD:
        return value[:_MAX_FIELD] + f"…[+{len(value) - _MAX_FIELD} chars]"
    return value


class Tracer:
    """Append-only JSONL tracer with size-based rollover.

    `max_bytes`/`backups` map straight onto RotatingFileHandler: the active file
    is `path`, older generations are `path.1` … `path.<backups>`, and anything
    past that is discarded. An isolated, non-propagating logger keeps trace lines
    out of the normal application log.
    """

    def __init__(
        self,
        path: str,
        *,
        max_bytes: int = 100 * 1024 * 1024,
        backups: int = 3,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self._logger: logging.Logger | None = None
        if not enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                self.path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            lg = logging.getLogger(f"ares.trace.{id(self)}")
            lg.setLevel(logging.INFO)
            lg.propagate = False  # never leak trace lines into the app log
            lg.handlers = [handler]
            self._logger = lg
            log.info("tracer: writing %s (cap=%dMB, keep=%d)", self.path, max_bytes // (1024 * 1024), backups)
        except Exception:
            log.exception("tracer: failed to open %s; tracing disabled", self.path)
            self.enabled = False

    def emit(self, kind: str, **fields: Any) -> None:
        """Write one trace record. Best-effort; never raises."""
        if not self.enabled or self._logger is None:
            return
        try:
            record: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
            }
            for k, v in fields.items():
                record[k] = _clip(v)
            self._logger.info(json.dumps(record, default=str, ensure_ascii=False))
        except Exception:
            log.exception("tracer: emit failed")


class NullTracer(Tracer):
    """A tracer that never writes — the default when tracing is disabled."""

    def __init__(self) -> None:
        self.path = Path("")
        self.enabled = False
        self._logger = None
