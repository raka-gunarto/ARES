"""Chrome DevTools Protocol client over `--remote-debugging-pipe` (spec §6.1).

Pipe mode frames each JSON message with a NUL byte. Chromium reads commands on
fd 3 and writes replies/events on fd 4; the launch command maps those onto the
browser runner's stdin/stdout, so the daemon drives a browser running as
`ares-browser` through its own audited sudo runner — no
debugging port is ever opened, and no WebSocket dependency is needed.

Every command carries a timeout. A browser that stops answering raises
`CDPError` instead of hanging the agent's worker (a single hung await there
blocks every message the household sends).
"""
from __future__ import annotations

import asyncio
import itertools
import json
from typing import Callable

from ares.core.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_COMMAND_TIMEOUT_S = 20.0
_READ_CHUNK = 1 << 16
# A single message larger than this means something is badly wrong (a
# screencast frame is ~100 KB); refuse rather than buffer without bound.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

EventHandler = Callable[[str, dict, "str | None"], None]


class CDPError(Exception):
    """A DevTools command failed, timed out, or the pipe closed."""


class BrowserError(Exception):
    """A browser action failed in a way worth telling the model/operator."""


class CDPConnection:
    """Request/response + event multiplexing over a NUL-framed byte stream."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    ) -> None:
        """Wrap a reader (browser → us) and writer (us → browser)."""
        self._reader = reader
        self._writer = writer
        self.timeout_s = timeout_s
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: list[EventHandler] = []
        self._read_task: asyncio.Task | None = None
        self.closed = asyncio.Event()

    def start(self) -> None:
        """Begin reading from the browser."""
        if self._read_task is None:
            self._read_task = asyncio.create_task(self._read_loop())

    def on_event(self, handler: EventHandler) -> None:
        """Register a sync callback for every protocol event."""
        self._handlers.append(handler)

    async def send(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout_s: float | None = None,
    ) -> dict:
        """Send one command and await its result."""
        if self.closed.is_set():
            raise CDPError("browser connection is closed")
        msg_id = next(self._ids)
        message: dict = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            self._writer.write(json.dumps(message).encode() + b"\0")
            await self._writer.drain()
        except (ConnectionError, OSError) as e:
            self._pending.pop(msg_id, None)
            self._mark_closed()
            raise CDPError(f"browser pipe write failed: {e}") from e
        try:
            return await asyncio.wait_for(future, timeout=timeout_s or self.timeout_s)
        except asyncio.TimeoutError as e:
            raise CDPError(f"{method} timed out") from e
        finally:
            self._pending.pop(msg_id, None)

    async def aclose(self) -> None:
        """Stop reading and fail anything still waiting."""
        if self._read_task is not None:
            self._read_task.cancel()
            await asyncio.gather(self._read_task, return_exceptions=True)
        try:
            self._writer.close()
        except (OSError, RuntimeError):
            pass
        self._mark_closed()

    def _mark_closed(self) -> None:
        self.closed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CDPError("browser connection closed"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        buffer = b""
        try:
            while True:
                chunk = await self._reader.read(_READ_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                while b"\0" in buffer:
                    raw, buffer = buffer.split(b"\0", 1)
                    if raw:
                        self._dispatch(raw)
                if len(buffer) > MAX_MESSAGE_BYTES:
                    logger.error("browser: oversized DevTools message; closing")
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("browser: DevTools read loop failed: %s", e)
        finally:
            self._mark_closed()

    def _dispatch(self, raw: bytes) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("browser: undecodable DevTools message dropped")
            return
        if "id" in message:
            future = self._pending.get(message["id"])
            if future is None or future.done():
                return
            if "error" in message:
                err = message["error"]
                future.set_exception(CDPError(str(err.get("message", err))))
            else:
                future.set_result(message.get("result") or {})
            return
        method = message.get("method")
        if not method:
            return
        params = message.get("params") or {}
        session_id = message.get("sessionId")
        for handler in self._handlers:
            try:
                handler(method, params, session_id)
            except Exception:
                logger.exception("browser: event handler failed for %s", method)
