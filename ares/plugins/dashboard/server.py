"""DashboardSource: runs the web dashboard (spec §17.1).

Owns the `WebChannel` and runs `uvicorn.Server.serve()` as a task in the
same asyncio event loop as the rest of the daemon (no separate process/
thread). `fastapi`/`uvicorn` are the `dashboard` extra and may not be
installed; this module must import cleanly without them, raising a clear
error only when a `DashboardSource` is actually constructed.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from ares.core.config import ConfigError
from ares.core.event import Event, EventBus, Priority
from ares.core.source import BaseSource
from ares.core.utils.ids import new_id
from ares.core.utils.logging import get_logger

log = get_logger(__name__)

# Guarded import of the dashboard extra (fastapi/uvicorn).
try:
    import uvicorn

    from ares.plugins.dashboard.api import build_app

    _HAVE_DASHBOARD = True
except ImportError:
    _HAVE_DASHBOARD = False


class DashboardSource(BaseSource):
    """Web dashboard event source (spec §17.1).

    Validates config, builds the FastAPI app via `build_app`, and runs
    uvicorn as a task on the current event loop. Owns the `WebChannel`
    passed in at construction (delivery is handled entirely by the
    channel; this class only starts/stops the HTTP server and emits
    `web_message` events for chat-in).
    """

    name = "dashboard"

    def __init__(
        self,
        bus: EventBus,
        config: dict,
        web_channel: Any,
        memory: Any,
        tasks: Any,
        priv_store: Any,
        prs_provider: Any = None,
        trace_file: Any = None,
        router: Any = None,
        subagent_manager: Any = None,
        home_provider: Any = None,
    ) -> None:
        """Initialize the dashboard source.

        Args:
            bus: The EventBus to publish events to.
            config: Dashboard configuration dict. Must contain a non-empty
                `password` (resolved from `!secret DASHBOARD_PASSWORD`
                upstream), used verbatim as the bearer token. `host`/`port`
                are optional (default "127.0.0.1"/8080).
            web_channel: WebChannel instance this source owns; passed to
                `build_app` and used for health queue-depth reporting.
            memory: BaseMemory instance, passed through to `build_app`.
            tasks: TaskStore instance, passed through to `build_app`.
            priv_store: PrivStore instance (or None), passed through to
                `build_app`.
            prs_provider: optional zero-arg callable returning the list of
                open self-edit PRs (§18); defaults to returning an empty list.
            trace_file: optional path to the activity-trace JSONL; the
                /api/trace endpoint tails it. None disables tracing.
            router: optional ResponseRouter, read for `last_channel` (where the
                most recent spoken reply actually landed) in /api/status.
            subagent_manager: optional SubagentManager (§20); its recent runs
                are surfaced at /api/subagents. None => that endpoint returns [].
            home_provider: optional async zero-arg callable () -> bool telling
                whether someone is home (the speaker channel's presence check),
                surfaced in /api/status. None => home reads as unknown.

        Raises:
            ConfigError: If `password` is missing/empty.
            RuntimeError: If the `dashboard` extra (fastapi/uvicorn) is not
                installed.
        """
        super().__init__(bus, config)
        if not _HAVE_DASHBOARD:
            raise RuntimeError(
                "dashboard: fastapi/uvicorn not installed; "
                "install the 'dashboard' extra to use DashboardSource"
            )
        # The shared dashboard password is used verbatim as the bearer token
        # (spec §17: "single shared password ... sent as a bearer token").
        token = config.get("password")
        if not token:
            raise ConfigError("dashboard: password is required")
        self.token = token
        self.host = str(config.get("host", "127.0.0.1"))
        self.port = int(config.get("port", 8080))

        self.web_channel = web_channel
        self.memory = memory
        self.tasks = tasks
        self.priv_store = priv_store
        # Zero-arg callable returning the list of open self-edit PRs (§18 cache);
        # defaults to an empty list when self-edit is disabled.
        self.prs_provider = prs_provider if prs_provider is not None else (lambda: [])
        # Path to the live activity-trace file (rotating JSONL), or None when
        # tracing is disabled; the /api/trace endpoint tails it.
        self.trace_file = trace_file
        # Optional collaborators feeding the presence badge and subagents panel.
        self.router = router
        self.subagent_manager = subagent_manager
        self.home_provider = home_provider

        self._start_time: float | None = None
        self._server: "uvicorn.Server | None" = None

    async def _emit_chat(self, text: str) -> None:
        """Publish a `web_message` event for user "primary".

        The dispatcher maps ("dashboard", "web_message") -> WEB, so the
        session's active channel flips to the dashboard automatically
        (spec §4.6); this method does not touch the channel itself.
        """
        event = Event(
            id=new_id(),
            source=self.name,
            type="web_message",
            payload={"text": text},
            priority=Priority.NORMAL,
            user_id="primary",
        )
        await self.bus.publish(event)

    def _health_provider(self) -> dict:
        """Return a small health/status dict for `GET /api/health`.

        Never raises: any failure computing queue depths degrades to an
        empty dict rather than breaking the health endpoint.
        """
        uptime = 0.0
        if self._start_time is not None:
            uptime = time.monotonic() - self._start_time
        queue_depths: dict[str, int] = {}
        try:
            queue_depths["web_buffer"] = self.web_channel.pending_count("primary")
        except Exception:
            log.exception("dashboard: failed to compute web_buffer depth")
        return {"ok": True, "uptime": uptime, "queue_depths": queue_depths}

    async def _status_provider(self) -> dict:
        """Where a reply would land right now: web presence, last channel, home.

        Never raises: a failing home read degrades to unknown (None) rather than
        breaking the status endpoint.
        """
        web_present = False
        try:
            web_present = self.web_channel.is_present("primary")
        except Exception:
            log.exception("dashboard: failed to read web presence")
        last_channel = None
        if self.router is not None:
            last_channel = getattr(self.router, "last_channel", {}).get("primary")
        home: bool | None = None
        if self.home_provider is not None:
            try:
                home = bool(await self.home_provider())
            except Exception:
                log.exception("dashboard: failed to read home presence")
                home = None
        return {"web_present": web_present, "last_channel": last_channel, "home": home}

    async def _subagents_provider(self) -> list[dict]:
        """Recent background runs for the subagents panel (spec §20).

        Never raises: any failure degrades to an empty list.
        """
        if self.subagent_manager is None:
            return []
        try:
            return await self.subagent_manager.list_all_runs("primary")
        except Exception:
            log.exception("dashboard: failed to list subagent runs")
            return []

    async def start(self) -> None:
        """Build the FastAPI app and run uvicorn until stopped.

        Blocks on `uvicorn.Server.serve()` for the lifetime of the source
        (like the other in-loop sources' `while not self._stopping` loops),
        so the supervisor's `await source.start()` supervises it directly:
        a mid-run uvicorn crash propagates here and triggers a restart, and
        `stop()` setting `should_exit` lets `serve()` return normally.
        """
        self._start_time = time.monotonic()
        static_dir = Path(__file__).parent / "static"

        app = build_app(
            token=self.token,
            emit_chat=self._emit_chat,
            web_channel=self.web_channel,
            memory=self.memory,
            tasks=self.tasks,
            priv_store=self.priv_store,
            prs_provider=self.prs_provider,
            health_provider=self._health_provider,
            static_dir=static_dir,
            trace_file=self.trace_file,
            status_provider=self._status_provider,
            subagents_provider=self._subagents_provider,
        )

        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        log.info("dashboard: serving on %s:%s", self.host, self.port)
        await self._server.serve()

    async def stop(self) -> None:
        """Signal uvicorn to exit so the serve() in start() returns.

        Setting `should_exit` lets `uvicorn.Server.serve()` return normally,
        which ends supervision cleanly. Never raises.
        """
        await super().stop()
        try:
            if self._server is not None:
                self._server.should_exit = True
        except Exception:
            log.exception("dashboard: error while stopping uvicorn server")
