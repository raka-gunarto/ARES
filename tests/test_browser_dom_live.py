"""The browser tool's in-page scripts, run in a real Chromium (spec §6.6).

Opt-in: skipped unless `ARES_TEST_CDP` points at a Chromium remote-debugging
endpoint, because the scripts need a real DOM with layout. For example:

    docker run -d --rm --name cdp --network host mcr.microsoft.com/playwright:v1.63.0-noble \\
      /ms-playwright/chromium-1243/chrome-linux64/chrome --headless=new --no-sandbox \\
      --remote-debugging-port=9333 --window-size=1280,900 about:blank
    ARES_TEST_CDP=http://127.0.0.1:9333 .venv/bin/pytest tests/test_browser_dom_live.py

Each page reproduces a failure from the live trace: a booking stalled on a
styled age checkbox and a 250-entry country dropdown, a job application whose
lower fields never appeared however far ARES scrolled, and a food order whose
"Add" button sat in a modal past the snapshot's truncation.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest

from ares.plugins.tools import browser_dom as dom
from ares.plugins.tools.browser_input import KEYS

CDP = os.environ.get("ARES_TEST_CDP", "")
pytestmark = pytest.mark.skipif(not CDP, reason="set ARES_TEST_CDP to a Chromium debugging URL")

FILLER = "".join(f"<p>Paragraph {i}: lorem ipsum dolor sit amet, consectetur.</p>" for i in range(400))
# Long enough that the open list overflows the snapshot, as the real one did.
COUNTRIES = [f"Republic of Country {i:03d}" for i in range(240)] + ["Ukraine", "United Kingdom", "United States"]
CHECKOUT = """<!doctype html><style>
 .box input{opacity:0;position:absolute} .vh{position:absolute;width:1px;height:1px;clip:rect(0 0 0 0)}
 #list{display:none;max-height:200px;overflow:auto} #list.open{display:block}</style>
<button id="country" onclick="document.getElementById('list').classList.toggle('open')">Select country</button>
<div id="list" role="listbox">%s</div>
<label class="box"><input type="checkbox" id="age"><span style="display:inline-block;width:14px;height:14px;border:1px solid"></span> I certify that I am at least 18</label>
<p><input type="checkbox" id="consent" class="vh"><label for="consent">I agree to the
<a href="https://example.org/terms">Terms</a></label></p>
<p><label><input type="checkbox" id="news"> Send me offers</label></p>
<script>document.querySelectorAll('[role=option]').forEach(o => o.onclick = () => {
  document.getElementById('country').textContent = o.dataset.v;
  document.getElementById('list').classList.remove('open'); });</script>""" % "".join(
    f'<div role="option" data-v="{c}"><span>{c}</span> <span>+{i}</span></div>'
    for i, c in enumerate(COUNTRIES))


class Page:
    """Just enough CDP: evaluate, navigate, and real input events."""

    async def __aenter__(self):
        import httpx
        websockets = pytest.importorskip("websockets")
        tabs = httpx.get(f"{CDP}/json/list").json()
        url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
        self.ws = await websockets.connect(url, max_size=None)
        self.n = 0
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def call(self, method, params=None):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if msg.get("id") == self.n:
                assert "error" not in msg, msg
                return msg["result"]

    async def ev(self, expr):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        assert "exceptionDetails" not in r, r
        return r["result"].get("value")

    async def load(self, html):
        data = base64.b64encode(html.encode()).decode()
        await self.call("Page.navigate", {"url": f"data:text/html;base64,{data}"})
        await asyncio.sleep(0.5)

    async def snapshot(self):
        return await self.ev(dom.SNAPSHOT_JS)

    async def click(self, ref):
        """Mirror Browser._click: toggle a hidden input, else a real mouse click."""
        loc = await self.ev(dom.locate_js(ref))
        assert loc, f"ref {ref} is stale"
        if loc.get("toggle"):
            await self.ev(dom.toggle_js(ref))
            return "toggle"
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await self.call("Input.dispatchMouseEvent", {"type": kind, "x": loc["x"], "y": loc["y"],
                                                         "button": "left", "clickCount": 1})
        return "mouse"

    async def key(self, name):
        key, code, vk, text = KEYS[name]
        await self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "code": code,
                                                   "windowsVirtualKeyCode": vk, "text": text})
        await self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code,
                                                   "windowsVirtualKeyCode": vk})


def ref_line(text, needle):
    line = next((ln for ln in text.splitlines() if needle in ln and ln.startswith("[")), None)
    return (int(line[1:line.index("]")]), line) if line else (None, None)


async def test_styled_checkboxes_are_exposed_and_toggle():
    async with Page() as p:
        await p.load(CHECKOUT)
        text = (await p.snapshot())["text"]
        ref, line = ref_line(text, "at least 18")
        assert line.endswith('checkbox "I certify that I am at least 18" (not checked)'), line
        assert await p.click(ref) == "toggle"
        assert await p.ev("document.getElementById('age').checked")
        assert ref_line((await p.snapshot())["text"], "at least 18")[1].endswith("(checked)")

        # The consent label holds a link: ticking it must not follow the link.
        ref, _ = ref_line((await p.snapshot())["text"], "I agree")
        before = await p.ev("location.href")
        assert await p.click(ref) == "toggle"
        assert await p.ev("document.getElementById('consent').checked")
        assert await p.ev("location.href") == before

        # A visible native checkbox is named by its label (was: checkbox "on").
        ref, line = ref_line((await p.snapshot())["text"], "Send me offers")
        assert line and await p.click(ref) == "mouse"
        assert await p.ev("document.getElementById('news').checked")


async def test_space_ticks_a_focused_checkbox():
    async with Page() as p:
        await p.load(CHECKOUT)
        await p.ev("document.getElementById('news').focus()")
        await p.key("Space")
        assert await p.ev("document.getElementById('news').checked")


async def test_click_text_reaches_an_option_the_snapshot_cut_off():
    async with Page() as p:
        await p.load(CHECKOUT)
        await p.click(ref_line((await p.snapshot())["text"], "Select country")[0])
        snap = await p.snapshot()
        assert "United Kingdom" not in snap["text"] and snap["truncated"]
        found = await p.ev(dom.find_text_js("United Kingdom"))
        assert found["matched"].startswith("United Kingdom")
        await p.click(0)
        assert await p.ev("document.getElementById('country').textContent") == "United Kingdom"
        assert "error" in await p.ev(dom.find_text_js("Narnia"))


async def test_click_text_picks_the_option_not_its_focusable_list():
    """Options as plain divs inside a tabindex list: clicking the list's centre
    would choose the wrong country."""
    opts = "".join(f'<div class="o" data-v="{c}">{c}</div>' for c in COUNTRIES)
    async with Page() as p:
        await p.load(f"<p id=out>none</p><div tabindex=-1 style='height:300px;overflow:auto'>{opts}</div>"
                     "<script>document.querySelectorAll('.o').forEach(o => o.onclick = () =>"
                     " document.getElementById('out').textContent = o.dataset.v)</script>")
        await p.ev(dom.find_text_js("United Kingdom"))
        await p.click(0)
        assert await p.ev("document.getElementById('out').textContent") == "United Kingdom"


async def test_scrolling_moves_the_snapshot_through_a_long_page():
    async with Page() as p:
        await p.load(f"<h1>Apply</h1>{FILLER}<input aria-label='late-field'><button>Submit</button>")
        top = await p.snapshot()
        assert "late-field" not in top["text"] and top["below"] > 0
        await p.ev("window.scrollTo(0, document.body.scrollHeight)")
        bottom = await p.snapshot()
        assert "late-field" in bottom["text"] and bottom["above"] > 0
        assert "Scroll to move through it" in dom.format_snapshot(bottom)


async def test_an_open_dialog_is_read_first():
    async with Page() as p:
        await p.load(f"<h1>Menu</h1>{FILLER}<div role='dialog' aria-modal='true' "
                     "style='position:fixed;top:50px;width:400px;height:200px'>"
                     "<h2>Zinger Burger</h2><button>Add to order</button></div>")
        text = (await p.snapshot())["text"]
        assert text.startswith("--- open dialog") and ref_line(text, "Add to order")[0]


async def test_fixed_header_stays_listed_when_windowed():
    async with Page() as p:
        await p.load("<header style='position:fixed;top:0'><a href='/b'>Basket (2)</a></header>"
                     f"{FILLER}<p id='mid'>MIDDLE</p>{FILLER}")
        await p.ev("document.getElementById('mid').scrollIntoView()")
        text = (await p.snapshot())["text"]
        assert "MIDDLE" in text and "Basket (2)" in text and "--- fixed on screen ---" in text


async def test_hidden_display_none_checkbox_via_its_label():
    async with Page() as p:
        await p.load("<style>#t{display:none}</style><input type=checkbox id=t>"
                     "<label for=t>Keep me signed in</label>")
        ref, _ = ref_line((await p.snapshot())["text"], "Keep me signed in")
        assert await p.click(ref) == "toggle"
        assert await p.ev("document.getElementById('t').checked")
