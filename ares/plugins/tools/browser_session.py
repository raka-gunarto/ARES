"""The persistent, stateful Chromium session (spec §6.1).

One long-lived browser, shared by the main agent's `browser` tool and the
operator's live view on the dashboard. Its profile persists on disk so sites
the operator logs into stay logged in across restarts.

Security shape (see also browser_proxy.py):

* Chromium runs as a dedicated `ares-browser` user through its own audited
  runner — not the daemon's uid, and not `ares-sbx`, because run_shell executes
  as ares-sbx and must not be able to read the login profile. prod refuses to
  launch without that separation.
* DevTools goes over a pipe on the runner's stdin/stdout: no debugging port.
* Every connection is forced through the vetting EgressProxy; QUIC and
  non-proxied WebRTC UDP are disabled so nothing routes around it.
* Downloads are denied.
"""
from __future__ import annotations

import asyncio
import os
import time

from ares.core.utils.logging import get_logger
from ares.plugins.tools.browser_cdp import BrowserError, CDPConnection, CDPError
from ares.plugins.tools.browser_input import operator_input as apply_operator_input
from ares.plugins.tools.browser_launch import (
    WINDOW_H,
    WINDOW_W,
    build_launch_command,
    separation_error,
)
from ares.plugins.tools.browser_proxy import EgressProxy

logger = get_logger(__name__)

BROWSER_RUNNER_PATH = "/usr/local/sbin/ares-browser-runner"
DEFAULT_PROFILE_DIR = ".ares-browser"
DEFAULT_IDLE_CLOSE_S = 1800
OPERATOR_TIMEOUT_S = 600
VIEWER_TIMEOUT_S = 15
LAUNCH_TIMEOUT_S = 30

__all__ = ["BrowserError", "BrowserSession"]


class BrowserSession:
    """Owns one persistent Chromium process and its DevTools connection."""

    def __init__(
        self,
        browser_user: str,
        workdir: str,
        binary: str = "",
        profile_dir: str = DEFAULT_PROFILE_DIR,
        idle_close_s: int = DEFAULT_IDLE_CLOSE_S,
        sandbox_user: str = "",
        runner_path: str = BROWSER_RUNNER_PATH,
    ) -> None:
        """Store launch configuration; nothing starts until first use."""
        self.browser_user = browser_user
        self.workdir = workdir
        self.binary = binary or "chromium"
        self.profile_dir = profile_dir or DEFAULT_PROFILE_DIR
        self.idle_close_s = idle_close_s
        self.sandbox_user = sandbox_user
        self.runner_path = runner_path
        # Held across a whole tool action so refs from one snapshot are not
        # invalidated by a concurrent operator click mid-action.
        self.lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._cdp: CDPConnection | None = None
        self._proxy: EgressProxy | None = None
        self._sid: str | None = None
        self._target_id: str | None = None
        self._url = ""
        self._title = ""
        self._load_event = asyncio.Event()
        self._frame: dict | None = None
        self._frame_seq = 0
        self._frame_event = asyncio.Event()
        self._screencasting = False
        self._viewer_last = 0.0
        self.last_activity = time.monotonic()
        self._operator_active = False
        self._operator_last = 0.0
        self._reaper: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()

    # --- launch -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._cdp is not None
            and not self._cdp.closed.is_set()
        )

    async def start(self) -> None:
        """Launch the browser if it isn't running. Raises BrowserError."""
        async with self._start_lock:
            if self.running:
                return
            reason = separation_error(self.browser_user, self.sandbox_user)
            if reason:
                logger.error("browser refused to launch: %s", reason)
                raise BrowserError(f"browser refused: {reason}")
            await self._teardown()

            self._proxy = EgressProxy()
            port = await self._proxy.start()
            command = build_launch_command(self.binary, self.profile_dir, port)
            if self.browser_user:
                argv = ["sudo", "-n", "-u", self.browser_user, self.runner_path, command]
                env, cwd = None, None
            else:
                logger.warning("browser: no browser_user; running as the daemon user (DEV ONLY)")
                argv = ["/bin/bash", "-lc", command]
                env = {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": self.workdir or "/tmp",
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                }
                cwd = self.workdir or None
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=env, cwd=cwd,
                    start_new_session=True,
                )
            except OSError as e:
                await self._teardown()
                raise BrowserError(f"failed to start browser: {e}") from e

            self._cdp = CDPConnection(self._proc.stdout, self._proc.stdin)
            self._cdp.on_event(self._on_event)
            self._cdp.start()
            try:
                await self._cdp.send("Target.setDiscoverTargets", {"discover": True},
                                     timeout_s=LAUNCH_TIMEOUT_S)
                await self._cdp.send("Browser.setDownloadBehavior", {"behavior": "deny"})
                pages = [t for t in (await self._cdp.send("Target.getTargets"))
                         .get("targetInfos", []) if t.get("type") == "page"]
                target = pages[0]["targetId"] if pages else (
                    await self._cdp.send("Target.createTarget", {"url": "about:blank"})
                )["targetId"]
                await self._attach(target)
            except CDPError as e:
                await self._teardown()
                raise BrowserError(
                    f"browser failed to start ({e}); is chromium installed and the "
                    "browser user provisioned?"
                ) from e
            self.touch()
            if self._reaper is None or self._reaper.done():
                self._reaper = asyncio.create_task(self._reap_loop())
            logger.info("browser: persistent session started")

    async def _attach(self, target_id: str) -> None:
        result = await self._cdp.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        self._sid, self._target_id = result["sessionId"], target_id
        await self._cdp.send("Page.enable", session_id=self._sid)
        # A headless page that isn't the focused one never renders: wheel input
        # (which waits on a frame) hangs and the screencast stays empty.
        await self._cdp.send("Page.bringToFront", session_id=self._sid)
        if self._screencasting:
            await self._start_screencast()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _on_event(self, method: str, params: dict, sid: str | None) -> None:
        if method == "Page.loadEventFired" and sid == self._sid:
            self._load_event.set()
        elif method == "Page.screencastFrame" and sid == self._sid:
            self._frame_seq += 1
            meta = params.get("metadata") or {}
            self._frame = {
                "seq": self._frame_seq, "data": params.get("data"),
                "width": meta.get("deviceWidth"), "height": meta.get("deviceHeight"),
            }
            self._frame_event.set()
            self._frame_event.clear()
            self._spawn(self._ack_frame(sid, params.get("sessionId")))
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo") or {}
            if info.get("targetId") == self._target_id:
                self._url, self._title = info.get("url", ""), info.get("title", "")
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            # A popup / target=_blank page: follow it, as a person would.
            if info.get("type") == "page" and info.get("openerId"):
                self._spawn(self._switch(info["targetId"]))
        elif method == "Target.targetDestroyed":
            if params.get("targetId") == self._target_id:
                self._spawn(self._recover_target())

    async def _ack_frame(self, sid: str, frame_session: int | None) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck",
                                 {"sessionId": frame_session}, session_id=sid)
        except (CDPError, AttributeError):
            pass

    async def _switch(self, target_id: str) -> None:
        try:
            await self._attach(target_id)
        except (CDPError, KeyError, AttributeError) as e:
            logger.debug("browser: could not follow new tab: %s", e)

    async def _recover_target(self) -> None:
        try:
            infos = (await self._cdp.send("Target.getTargets")).get("targetInfos", [])
            pages = [t["targetId"] for t in infos if t.get("type") == "page"]
            target = pages[-1] if pages else (
                await self._cdp.send("Target.createTarget", {"url": "about:blank"})
            )["targetId"]
            await self._attach(target)
        except (CDPError, KeyError, AttributeError) as e:
            logger.debug("browser: could not recover a page target: %s", e)

    # --- page primitives ------------------------------------------------------

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def page(self, method: str, params: dict | None = None,
                   timeout_s: float | None = None) -> dict:
        """Send a command to the active page. Raises BrowserError."""
        if not self.running or not self._sid:
            raise BrowserError("the browser is not running")
        try:
            return await self._cdp.send(method, params, session_id=self._sid,
                                        timeout_s=timeout_s)
        except CDPError as e:
            raise BrowserError(str(e)) from e

    async def evaluate(self, expression: str):
        """Evaluate JS in the page and return its JSON value."""
        result = await self.page("Runtime.evaluate",
                                 {"expression": expression, "returnByValue": True})
        if result.get("exceptionDetails"):
            text = result["exceptionDetails"].get("text", "script error")
            raise BrowserError(f"page script failed: {text}")
        return (result.get("result") or {}).get("value")

    def expect_load(self) -> None:
        """Arm the load wait before an action that may navigate."""
        self._load_event.clear()

    async def settle(self, seconds: float = 0.8) -> None:
        await asyncio.sleep(seconds)

    async def navigate(self, url: str) -> None:
        self.expect_load()
        result = await self.page("Page.navigate", {"url": url}, timeout_s=30)
        if result.get("errorText"):
            raise BrowserError(f"navigation failed: {result['errorText']}")
        await self.wait_loaded()

    async def wait_loaded(self, max_s: float = 15.0) -> None:
        """Wait for the load event (bounded), then a short settle."""
        try:
            await asyncio.wait_for(self._load_event.wait(), timeout=max_s)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.5)

    # --- operator & live view ------------------------------------------------

    def set_operator(self, active: bool) -> None:
        self._operator_active = active
        self._operator_last = time.monotonic()

    @property
    def operator_in_control(self) -> bool:
        if self._operator_active and time.monotonic() - self._operator_last > OPERATOR_TIMEOUT_S:
            self._operator_active = False  # walked away: hand it back to ARES
        return self._operator_active

    async def operator_input(self, body: dict) -> None:
        """Apply a live-view input event; using the browser takes control.

        Deliberately does not wait for `lock`: the operator always wins. An
        agent action already under way may see its refs go stale and fail;
        the next one is refused while the operator is in control.
        """
        if not self.running:
            raise BrowserError("the browser is not running")
        self.set_operator(True)
        await apply_operator_input(self, body)

    async def frame_since(self, seq: int, wait_s: float = 10.0) -> dict | None:
        """The newest frame after `seq`, waiting briefly for one."""
        self._viewer_last = time.monotonic()
        if not self.running:
            return None
        if not self._screencasting:
            await self._start_screencast()
        if self._frame and self._frame["seq"] > seq:
            return self._frame
        try:
            await asyncio.wait_for(self._frame_event.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            pass
        return self._frame if self._frame and self._frame["seq"] > seq else None

    async def _start_screencast(self) -> None:
        self._screencasting = True
        try:
            await self.page("Page.startScreencast", {
                "format": "jpeg", "quality": 60,
                "maxWidth": WINDOW_W, "maxHeight": WINDOW_H, "everyNthFrame": 1,
            })
        except BrowserError as e:
            logger.debug("browser: screencast start failed: %s", e)

    async def _stop_screencast(self) -> None:
        self._screencasting = False
        try:
            await self.page("Page.stopScreencast")
        except BrowserError:
            pass

    def state(self) -> dict:
        return {
            "running": self.running, "url": self._url if self.running else "",
            "title": self._title if self.running else "",
            "operator_in_control": self.operator_in_control, "frame_seq": self._frame_seq,
        }

    # --- shutdown -------------------------------------------------------------

    async def close(self) -> None:
        """Close the browser gracefully (so cookies flush to the profile)."""
        async with self._start_lock:
            await self._teardown()

    async def _teardown(self) -> None:
        if self._cdp is not None and not self._cdp.closed.is_set():
            try:
                await self._cdp.send("Browser.close", timeout_s=5)
            except CDPError:
                pass
        # Closing the DevTools pipe makes Chromium exit on its own. That matters:
        # the browser runs as ares-browser, which the daemon cannot signal, so
        # terminate() below only reaches sudo (which relays SIGTERM; a SIGKILL
        # would orphan the browser, so it is never sent).
        if self._cdp is not None:
            await self._cdp.aclose()
        if self._proc is not None and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    logger.warning("browser: process did not exit after close")
        if self._proxy is not None:
            await self._proxy.aclose()
        self._proc = self._cdp = self._proxy = None
        self._sid = self._target_id = None
        self._screencasting = False
        self._frame = None

    async def aclose(self) -> None:
        """Daemon shutdown: stop the reaper and close the browser."""
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        await self.close()

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                if not self.running:
                    continue
                now = time.monotonic()
                if self._screencasting and now - self._viewer_last > VIEWER_TIMEOUT_S:
                    await self._stop_screencast()
                last = max(self.last_activity, self._operator_last, self._viewer_last)
                if now - last > self.idle_close_s and not self.lock.locked():
                    logger.info("browser: closing idle session (profile kept)")
                    await self.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("browser: reaper tick failed")
