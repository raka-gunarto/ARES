"""Dashboard routes for the persistent browser's live view (spec §17, §6.6).

The operator watches the page ARES is using and can take the wheel — above
all to sign in to a site themselves, so no password ever passes through the
model. The session object is injected by instance wiring (plugins never import
each other); this module only needs its small duck-typed surface:
`state()`, `start()`, `close()`, `frame_since(seq, wait_s)`,
`set_operator(active)`, and `operator_input(body)`. Every route is behind the
dashboard bearer token.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ares.core.utils.logging import get_logger

logger = get_logger(__name__)

FRAME_WAIT_S = 10.0


def _error(e: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": str(e)})


def register_browser_routes(app: FastAPI, api_auth: Any, browser: Any) -> None:
    """Attach /api/browser/* to `app`; `api_auth` is the token dependency."""

    async def _body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @app.get("/api/browser/state", dependencies=[api_auth])
    async def browser_state() -> JSONResponse:
        """Whether the browser runs, its page, and who is in control."""
        return JSONResponse(browser.state())

    @app.post("/api/browser/launch", dependencies=[api_auth])
    async def browser_launch() -> JSONResponse:
        """Start the browser (e.g. to sign in before asking ARES to use a site)."""
        try:
            await browser.start()
        except Exception as e:  # BrowserError: surface the reason to the operator
            logger.warning("dashboard: browser launch failed: %s", e)
            return _error(e, 502)
        return JSONResponse(browser.state())

    @app.post("/api/browser/close", dependencies=[api_auth])
    async def browser_close() -> JSONResponse:
        """Close the browser; the on-disk profile (logins) is kept."""
        browser.set_operator(False)
        await browser.close()
        return JSONResponse(browser.state())

    @app.get("/api/browser/frame", dependencies=[api_auth])
    async def browser_frame(since: int = 0) -> JSONResponse:
        """Long-poll for the next screencast frame after `since`.

        Screencasting only runs while someone polls here, so an unwatched
        browser costs no encoding work.
        """
        frame = await browser.frame_since(since, wait_s=FRAME_WAIT_S)
        return JSONResponse({"frame": frame, "state": browser.state()})

    @app.post("/api/browser/control", dependencies=[api_auth])
    async def browser_control(request: Request) -> JSONResponse:
        """Take control from ARES (`active: true`) or hand it back."""
        body = await _body(request)
        browser.set_operator(bool(body.get("active")))
        return JSONResponse(browser.state())

    @app.post("/api/browser/input", dependencies=[api_auth])
    async def browser_input(request: Request) -> JSONResponse:
        """One click/text/key/scroll/navigate/back/forward from the live view."""
        try:
            await browser.operator_input(await _body(request))
        except Exception as e:  # BrowserError: bad input or a page that refused
            return _error(e)
        return JSONResponse({"ok": True})
