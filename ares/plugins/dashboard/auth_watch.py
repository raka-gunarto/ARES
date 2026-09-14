"""Report dashboard requests that arrive without a valid token (spec §17.4).

The dashboard is reachable from the internet through the tunnel, so a request
without the right bearer token is either the operator mistyping the password or
someone who should not be there. Every such request is logged; the first one
from each client (per kind) in a cooldown window is also pushed to the
operator's phone when an alert callable is wired (ntfy, if a topic is set).

`/` and `/api/version` are the lock screen: the operator's own browser fetches
them before it has a token, so they are only reported when a WRONG token is
sent. Everything else without a valid token is reported.

Pure ASGI middleware — no BaseHTTPMiddleware, which would buffer and break the
long-poll routes. The bad token itself is never logged or pushed: it is most
likely a typo of the real password.
"""
from __future__ import annotations

import asyncio
import hmac
import time
from collections import deque
from typing import Awaitable, Callable

from ares.core.utils.logging import get_logger

logger = get_logger(__name__)

PUBLIC_PATHS = frozenset({"/", "/api/version"})
NOTIFY_COOLDOWN_S = 900
MAX_NOTIFY_PER_HOUR = 12
MAX_TRACKED = 1024
NOTIFY_TIMEOUT_S = 15
_FIELD_MAX = 200

Alert = Callable[[str, str], Awaitable[object]]


def _clip(value: str) -> str:
    return value if len(value) <= _FIELD_MAX else value[:_FIELD_MAX] + "…"


class AuthWatch:
    """Classify requests, log failures, and rate-limit operator alerts."""

    def __init__(
        self,
        token: str,
        alert: Alert | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`alert(title, message)` pushes to the operator; None = log only."""
        self._expected = f"Bearer {token}".encode()
        self.alert = alert
        self._clock = clock
        # (kind, client) -> [last alert time, requests suppressed since then]
        self._seen: dict[tuple[str, str], list] = {}
        self._sent: deque[float] = deque()
        self._tasks: set[asyncio.Task] = set()

    def classify(self, path: str, auth: bytes | None) -> str | None:
        """'bad token', 'unauthenticated', or None for a request that's fine."""
        if auth is not None:
            return None if hmac.compare_digest(auth, self._expected) else "bad token"
        return None if path in PUBLIC_PATHS else "unauthenticated"

    def observe(self, scope: dict) -> None:
        """Inspect one HTTP request scope; never raises into the request."""
        try:
            headers = dict(scope.get("headers") or [])
            path = scope.get("path") or ""
            kind = self.classify(path, headers.get(b"authorization"))
            if kind is None:
                return
            peer = (scope.get("client") or ("?", 0))[0]
            # Set by Cloudflare at the tunnel edge; the peer is then cloudflared.
            ip = headers.get(b"cf-connecting-ip", b"").decode("latin-1").strip() or peer
            ua = _clip(headers.get(b"user-agent", b"").decode("latin-1"))
            method = scope.get("method", "?")
            logger.warning(
                "dashboard auth: %s: %s %r from ip=%s peer=%s ua=%r",
                kind, method, _clip(path), _clip(ip), peer, ua,
            )
            self._maybe_alert(kind, ip, f"{method} {_clip(path)} from {_clip(ip)}\n{ua}")
        except Exception:
            logger.exception("dashboard auth: failed to inspect request")

    def _maybe_alert(self, kind: str, client: str, detail: str) -> None:
        if self.alert is None:
            return
        now = self._clock()
        key = (kind, client)
        entry = self._seen.get(key)
        if entry is not None and now - entry[0] < NOTIFY_COOLDOWN_S:
            entry[1] += 1
            return
        while self._sent and now - self._sent[0] >= 3600:
            self._sent.popleft()
        if len(self._sent) >= MAX_NOTIFY_PER_HOUR:
            logger.warning("dashboard auth: alert cap reached; logging only")
            return
        suppressed = entry[1] if entry is not None else 0
        if len(self._seen) >= MAX_TRACKED:
            self._prune(now)
        self._seen[key] = [now, 0]
        self._sent.append(now)
        if suppressed:
            detail += f"\n(+{suppressed} more from this client since the last alert)"
        task = asyncio.get_running_loop().create_task(
            self._send(f"ARES dashboard: {kind}", detail)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._seen.items() if now - v[0] >= NOTIFY_COOLDOWN_S]:
            del self._seen[key]
        # Still full (many clients inside one window): forget the oldest.
        while len(self._seen) >= MAX_TRACKED:
            del self._seen[min(self._seen, key=lambda k: self._seen[k][0])]

    async def _send(self, title: str, message: str) -> None:
        try:
            await asyncio.wait_for(self.alert(title, message), timeout=NOTIFY_TIMEOUT_S)
        except Exception:
            logger.exception("dashboard auth: alert failed")


class AuthWatchMiddleware:
    """ASGI middleware feeding every HTTP request to an `AuthWatch`."""

    def __init__(self, app, watch: AuthWatch) -> None:
        """Wrap `app`."""
        self.app = app
        self.watch = watch

    async def __call__(self, scope, receive, send) -> None:
        """Observe, then pass the request through untouched."""
        if scope.get("type") == "http":
            self.watch.observe(scope)
        await self.app(scope, receive, send)
