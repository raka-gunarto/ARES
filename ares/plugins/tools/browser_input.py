"""Input primitives for the stateful browser (spec §6.6).

Real DevTools input events rather than `element.click()` in JS, so pages see
the same trusted events a person produces. Shared by the agent's `browser`
tool and the operator's live view.
"""
from __future__ import annotations

import typing

from ares.plugins.tools.browser_cdp import BrowserError
from ares.plugins.tools.browser_launch import WINDOW_H, WINDOW_W
from ares.plugins.tools.browser_proxy import validate_url

if typing.TYPE_CHECKING:
    from ares.plugins.tools.browser_session import BrowserSession

# name -> (key, code, windowsVirtualKeyCode, text)
KEYS = {
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Tab": ("Tab", "Tab", 9, None),
    "Backspace": ("Backspace", "Backspace", 8, None),
    "Delete": ("Delete", "Delete", 46, None),
    "Escape": ("Escape", "Escape", 27, None),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, None),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, None),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, None),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, None),
    "PageUp": ("PageUp", "PageUp", 33, None),
    "PageDown": ("PageDown", "PageDown", 34, None),
    "Home": ("Home", "Home", 36, None),
    "End": ("End", "End", 35, None),
}


async def mouse_click(session: BrowserSession, x: float, y: float) -> None:
    base = {"x": x, "y": y, "button": "left", "clickCount": 1}
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        await session.page("Input.dispatchMouseEvent", {**base, "type": kind})


async def insert_text(session: BrowserSession, text: str) -> None:
    await session.page("Input.insertText", {"text": text})


async def press_key(session: BrowserSession, name: str) -> None:
    if name not in KEYS:
        raise BrowserError(f"unsupported key {name!r}")
    key, code, vk, text = KEYS[name]
    down = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": code,
            "windowsVirtualKeyCode": vk}
    if text:
        down["text"] = text
    await session.page("Input.dispatchKeyEvent", down)
    await session.page("Input.dispatchKeyEvent",
                       {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk})


async def scroll(session: BrowserSession, dy: float) -> None:
    await session.page("Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": WINDOW_W / 2, "y": WINDOW_H / 2,
        "deltaX": 0, "deltaY": dy,
    })
    await session.settle(0.4)


async def history(session: BrowserSession, delta: int) -> None:
    hist = await session.page("Page.getNavigationHistory")
    index = hist.get("currentIndex", 0) + delta
    entries = hist.get("entries") or []
    if not 0 <= index < len(entries):
        raise BrowserError("no page in that direction of the history")
    session.expect_load()
    await session.page("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})
    await session.wait_loaded()


MAX_OPERATOR_TEXT = 4096


def _coord(value, limit: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BrowserError("click needs numeric x and y") from None
    return min(max(number, 0.0), float(limit))


async def operator_input(session: BrowserSession, body: dict) -> None:
    """Apply one input event from the operator's live view.

    Coordinates are CSS pixels of the page viewport (the frontend maps the
    scaled screencast image back using the frame's width/height). Navigation
    goes through the same vetting as the agent's `open`.
    """
    kind = body.get("type")
    if kind == "click":
        await mouse_click(session, _coord(body.get("x"), WINDOW_W), _coord(body.get("y"), WINDOW_H))
    elif kind == "text":
        text = str(body.get("text", ""))[:MAX_OPERATOR_TEXT]
        if text:
            await insert_text(session, text)
    elif kind == "key":
        await press_key(session, str(body.get("key", "")))
    elif kind == "scroll":
        try:
            dy = max(min(float(body.get("dy", 0)), 5000.0), -5000.0)
        except (TypeError, ValueError):
            raise BrowserError("scroll needs a numeric dy") from None
        await session.page("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": WINDOW_W / 2, "y": WINDOW_H / 2,
            "deltaX": 0, "deltaY": dy,
        })
    elif kind == "navigate":
        url = str(body.get("url", "")).strip()
        if url and "://" not in url:
            url = "https://" + url
        reason = await validate_url(url)
        if reason:
            raise BrowserError(reason)
        # Don't wait for the load: the operator watches it happen.
        result = await session.page("Page.navigate", {"url": url}, timeout_s=30)
        if result.get("errorText"):
            raise BrowserError(f"navigation failed: {result['errorText']}")
    elif kind in ("back", "forward"):
        hist = await session.page("Page.getNavigationHistory")
        index = hist.get("currentIndex", 0) + (-1 if kind == "back" else 1)
        entries = hist.get("entries") or []
        if 0 <= index < len(entries):
            await session.page("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})
    else:
        raise BrowserError(f"unknown input type {kind!r}")
