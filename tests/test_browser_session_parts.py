"""Stateful browser (§6.6): egress proxy, DevTools pipe, launch template."""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from ares.plugins.tools import browser_launch
from ares.plugins.tools.browser_cdp import CDPConnection, CDPError
from ares.plugins.tools.browser_proxy import EgressProxy, validate_url, vet_destination


# --- egress proxy -------------------------------------------------------------


async def _upstream():
    """A tiny server that records what it receives and answers once."""
    received: list[bytes] = []

    async def handle(reader, writer):
        received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], received


async def _ask(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    return data


async def test_proxy_refuses_loopback_with_the_real_policy():
    proxy = EgressProxy(allowed_ports=frozenset({443}))
    port = await proxy.start()
    try:
        reply = await _ask(port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403")
        assert b"private/internal" in reply
        reply = await _ask(port, b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert b"403" in reply.split(b"\r\n")[0]
    finally:
        await proxy.aclose()


async def test_proxy_refuses_non_web_ports():
    proxy = EgressProxy(is_forbidden=lambda ip: False)
    port = await proxy.start()
    try:
        reply = await _ask(port, b"CONNECT example.com:25 HTTP/1.1\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403") and b"not a web port" in reply
    finally:
        await proxy.aclose()


async def test_proxy_connect_tunnels_to_the_vetted_address():
    server, up_port, received = await _upstream()
    proxy = EgressProxy(is_forbidden=lambda ip: False, allowed_ports=frozenset({up_port}))
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 5)).endswith(b"hi")
        writer.close()
        assert received and received[0].startswith(b"GET / HTTP/1.1")
    finally:
        await proxy.aclose()
        server.close()


async def test_proxy_rewrites_absolute_uri_and_strips_hop_headers():
    server, up_port, received = await _upstream()
    proxy = EgressProxy(is_forbidden=lambda ip: False, allowed_ports=frozenset({up_port}))
    port = await proxy.start()
    try:
        reply = await _ask(port, (
            f"GET http://127.0.0.1:{up_port}/a/b?q=1 HTTP/1.1\r\nHost: t\r\n"
            "Proxy-Connection: keep-alive\r\nProxy-Authorization: x\r\n\r\n"
        ).encode())
        assert reply.endswith(b"hi")
        head = received[0].decode()
        assert head.startswith("GET /a/b?q=1 HTTP/1.1\r\n")
        assert "Proxy-" not in head and "Connection: close" in head
    finally:
        await proxy.aclose()
        server.close()


async def test_proxy_caps_concurrent_connections(monkeypatch):
    monkeypatch.setattr("ares.plugins.tools.browser_proxy.MAX_CONNECTIONS", 0)
    proxy = EgressProxy(is_forbidden=lambda ip: False)
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        reply = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        assert reply.startswith(b"HTTP/1.1 503")
    finally:
        await proxy.aclose()


async def test_vet_rejects_a_name_with_any_private_answer():
    addresses, reason = await vet_destination(
        "localhost", 443, is_forbidden=lambda ip: ip.startswith("127.") or ip == "::1"
    )
    assert addresses == [] and "private" in reason


@pytest.mark.parametrize("url, needle", [
    ("", "required"),
    ("https://exa mple.com", "whitespace"),
    ("ftp://example.com/", "http(s)"),
    ("file:///etc/passwd", "http(s)"),
    ("http://127.0.0.1/", "private"),
    ("http://[::1]/", "private"),
    ("http://10.0.0.5/", "private"),
    ("https://example.com:22/", "web port"),
    ("https://example.com:99999/", "invalid port"),
])
async def test_validate_url_refusals(url, needle):
    reason = await validate_url(url)
    assert reason and needle in reason


# --- DevTools pipe --------------------------------------------------------------


class _Writer:
    def __init__(self):
        self.sent: list[dict] = []

    def write(self, data: bytes):
        assert data.endswith(b"\0")
        self.sent.append(json.loads(data[:-1]))

    async def drain(self):
        pass

    def close(self):
        pass


async def test_cdp_matches_replies_errors_and_events():
    reader, writer = asyncio.StreamReader(), _Writer()
    cdp = CDPConnection(reader, writer, timeout_s=2)
    events = []
    cdp.on_event(lambda m, p, s: events.append((m, p, s)))
    cdp.start()

    pending = asyncio.create_task(cdp.send("Page.navigate", {"url": "u"}, session_id="S1"))
    await asyncio.sleep(0)
    msg = writer.sent[0]
    assert msg["method"] == "Page.navigate" and msg["sessionId"] == "S1"
    # An event and the reply arrive split across chunks, NUL-framed.
    frame = json.dumps({"method": "Page.loadEventFired", "params": {"t": 1}, "sessionId": "S1"})
    reply = json.dumps({"id": msg["id"], "result": {"frameId": "F"}})
    blob = (frame + "\0" + reply + "\0").encode()
    reader.feed_data(blob[:10])
    reader.feed_data(blob[10:])
    assert await pending == {"frameId": "F"}
    assert events == [("Page.loadEventFired", {"t": 1}, "S1")]

    failing = asyncio.create_task(cdp.send("Bad.method"))
    await asyncio.sleep(0)
    reader.feed_data(json.dumps({"id": writer.sent[1]["id"], "error": {"message": "nope"}}).encode() + b"\0")
    with pytest.raises(CDPError, match="nope"):
        await failing
    await cdp.aclose()


async def test_cdp_times_out_instead_of_hanging():
    cdp = CDPConnection(asyncio.StreamReader(), _Writer(), timeout_s=0.05)
    cdp.start()
    with pytest.raises(CDPError, match="timed out"):
        await cdp.send("Runtime.evaluate")
    await cdp.aclose()


async def test_cdp_closing_pipe_fails_pending_and_later_sends():
    reader = asyncio.StreamReader()
    cdp = CDPConnection(reader, _Writer(), timeout_s=5)
    cdp.start()
    pending = asyncio.create_task(cdp.send("Page.enable"))
    await asyncio.sleep(0)
    reader.feed_eof()
    with pytest.raises(CDPError, match="closed"):
        await pending
    with pytest.raises(CDPError, match="closed"):
        await cdp.send("Page.enable")


# --- launch template & separation ----------------------------------------------


def test_launch_forces_all_traffic_through_the_proxy():
    cmd = browser_launch.build_launch_command("/usr/bin/chromium", ".ares-browser", 41234)
    for flag in (
        "--proxy-server=http://127.0.0.1:41234",
        "'--proxy-bypass-list=<-loopback>'",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--remote-debugging-pipe",
        "--headless=new",
    ):
        assert flag in cmd, flag
    assert "--remote-debugging-port" not in cmd
    assert '--user-data-dir="$HOME"/.ares-browser' in cmd
    assert cmd.rstrip().endswith("3<&0 4>&1 0</dev/null 1>/dev/null 2>/dev/null")


def _run_launch(tmp_path, version_output: str) -> list[str]:
    """Run the real template in bash against a fake chromium; return its argv."""
    fake = tmp_path / "chromium"
    fake.write_text(
        "#!/bin/bash\n"
        f"if [ \"$1\" = --version ]; then {version_output}; exit 0; fi\n"
        'printf "%s\\n" "$@" > "$ARGS_OUT"\n'
    )
    fake.chmod(0o755)
    out = tmp_path / "argv"
    cmd = browser_launch.build_launch_command(str(fake), str(tmp_path / "prof"), 1)
    subprocess.run(["/bin/bash", "-c", cmd], env={"PATH": "/usr/bin:/bin", "ARGS_OUT": str(out)},
                   stdin=subprocess.DEVNULL, check=True, timeout=10)
    return out.read_text().splitlines()


def test_launch_presents_as_ordinary_chromium_of_the_installed_version(tmp_path):
    """Cloudflare-style checks turn away HeadlessChrome / navigator.webdriver."""
    argv = _run_launch(tmp_path, "echo 'Chromium 151.0.7922.173 built on Debian'")
    ua = next(a for a in argv if a.startswith("--user-agent="))
    assert "Chrome/151.0.0.0 " in ua and "Headless" not in ua
    assert "--disable-blink-features=AutomationControlled" in argv
    assert "--screen-info={1280x900}" in argv
    assert "--proxy-bypass-list=<-loopback>" in argv  # still parsed as one word


def test_launch_user_agent_falls_back_when_version_is_unreadable(tmp_path):
    argv = _run_launch(tmp_path, "echo garbage")
    ua = next(a for a in argv if a.startswith("--user-agent="))
    assert f"Chrome/{browser_launch.FALLBACK_MAJOR}.0.0.0 " in ua


def test_launch_quotes_hostile_profile_paths():
    cmd = browser_launch.build_launch_command("chromium", "/tmp/a b;rm -rf ~", 1)
    assert "'/tmp/a b;rm -rf ~'" in cmd


def test_separation_is_enforced_only_in_prod(monkeypatch):
    monkeypatch.setattr(browser_launch.getpass, "getuser", lambda: "ares")
    monkeypatch.setenv("ARES_ENV", "dev")
    assert browser_launch.separation_error("", "") is None
    monkeypatch.setenv("ARES_ENV", "prod")
    assert "no dedicated browser user" in browser_launch.separation_error("", "ares-sbx")
    assert "no dedicated browser user" in browser_launch.separation_error("ares", "ares-sbx")
    assert "differ" in browser_launch.separation_error("ares-sbx", "ares-sbx")
    assert browser_launch.separation_error("ares-browser", "ares-sbx") is None
