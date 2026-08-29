"""Tests for the sandboxed headless-browser tool (spec §6.1, §15).

The security-relevant properties are the point of this file: a model-supplied
URL must never reach an internal address, never break out of argv into a shell,
and never run as the daemon's own uid in prod.
"""
from __future__ import annotations

import shlex

import pytest

from ares.core.tool import ToolContext
from ares.plugins.tools import browser_tools
from ares.plugins.tools.browser_tools import (
    FetchPage,
    build_browser_tools,
    extract_text,
    resolve_public_host,
)


def _tool(**kw) -> FetchPage:
    return FetchPage(kw.pop("sandbox_user", "ares-sbx"), kw.pop("workdir", "/home/ares-sbx"), **kw)


def _ctx():
    return ToolContext(
        user_id="primary", event=None, session=None, router=None,
        memory=None, tasks=None, registry=None, services={},
    )


def _fake_resolve(mapping):
    """Patch DNS so tests never touch the network."""
    def _resolve(host, port, **kw):
        if host not in mapping:
            import socket
            raise socket.gaierror("no such host")
        return [(2, 1, 6, "", (ip, port)) for ip in mapping[host]]
    return _resolve


# ---- SSRF: the internal network must be unreachable ------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "10.16.0.1",       # the TAP gateway: Home Assistant, Asterisk
        "10.16.0.2",       # the guest itself: dashboard + updater hook
        "127.0.0.1",       # loopback
        "169.254.169.254", # link-local / cloud metadata
        "192.168.1.64",    # host LAN
        "172.16.0.1",      # pterodactyl bridge
        "::1",             # loopback v6
        "fd00::1",         # unique-local v6
    ],
)
async def test_private_addresses_are_refused(monkeypatch, ip):
    monkeypatch.setattr(
        browser_tools.socket, "getaddrinfo", _fake_resolve({"evil.test": [ip]})
    )
    result = await _tool().run(_ctx(), url="http://evil.test/")
    assert not result.ok
    assert "private/internal" in result.content


async def test_any_private_answer_rejects_the_whole_name(monkeypatch):
    """A name resolving to public AND private is the classic SSRF bypass."""
    monkeypatch.setattr(
        browser_tools.socket,
        "getaddrinfo",
        _fake_resolve({"split.test": ["93.184.216.34", "10.16.0.1"]}),
    )
    result = await _tool().run(_ctx(), url="https://split.test/")
    assert not result.ok
    assert "10.16.0.1" in result.content


def test_public_host_resolves_and_pins():
    ip, err = None, None
    import socket as _s
    orig = browser_tools.socket.getaddrinfo
    browser_tools.socket.getaddrinfo = _fake_resolve({"ok.test": ["93.184.216.34"]})
    try:
        ip, err = resolve_public_host("ok.test", 443)
    finally:
        browser_tools.socket.getaddrinfo = orig
    assert err is None
    assert ip == "93.184.216.34"


# ---- URL validation --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
    ],
)
async def test_non_http_schemes_refused(url):
    result = await _tool().run(_ctx(), url=url)
    assert not result.ok
    assert "only http/https" in result.content


async def test_empty_and_hostless_urls_refused():
    assert not (await _tool().run(_ctx(), url="")).ok
    assert not (await _tool().run(_ctx(), url="http:///nohost")).ok


async def test_url_with_whitespace_refused():
    """Whitespace is how a URL becomes two argv words."""
    result = await _tool().run(_ctx(), url="http://ok.test/ --user-data-dir=/etc")
    assert not result.ok
    assert "whitespace" in result.content


# ---- command construction: no shell breakout -------------------------------


def test_url_is_shell_quoted_in_the_command():
    cmd = _tool()._build_command(
        "https://ok.test/?q=x';rm -rf ~;'", "93.184.216.34", 20000
    )
    # The dangerous text survives only inside a single quoted argv word.
    assert "rm -rf ~" in cmd
    assert shlex.quote("https://ok.test/?q=x';rm -rf ~;'") in cmd
    # And the whole thing still parses as the argv we intended.
    parts = shlex.split(cmd.split("&&", 1)[1].split("; rc=", 1)[0])
    assert parts[-1] == "https://ok.test/?q=x';rm -rf ~;'"


def test_command_pins_the_vetted_address():
    cmd = _tool()._build_command("https://ok.test/", "93.184.216.34", 20000)
    assert "MAP ok.test 93.184.216.34" in cmd


def test_command_uses_a_throwaway_profile():
    cmd = _tool()._build_command("https://ok.test/", "93.184.216.34", 20000)
    assert "mktemp -d" in cmd and "--user-data-dir=$d" in cmd
    assert "rm -rf $d" in cmd, "profile must not persist cookies between fetches"


# ---- privilege separation (§15) --------------------------------------------


async def test_refuses_to_run_as_daemon_user_in_prod(monkeypatch):
    monkeypatch.setenv("ARES_ENV", "prod")
    monkeypatch.setattr(
        browser_tools.socket, "getaddrinfo", _fake_resolve({"ok.test": ["93.184.216.34"]})
    )
    monkeypatch.setattr(browser_tools.getpass, "getuser", lambda: "ares")
    result = await _tool(sandbox_user="ares").run(_ctx(), url="https://ok.test/")
    assert not result.ok
    assert "sandbox user separation" in result.content


async def test_prod_without_sandbox_user_is_refused(monkeypatch):
    monkeypatch.setenv("ARES_ENV", "prod")
    monkeypatch.setattr(
        browser_tools.socket, "getaddrinfo", _fake_resolve({"ok.test": ["93.184.216.34"]})
    )
    result = await _tool(sandbox_user="").run(_ctx(), url="https://ok.test/")
    assert not result.ok
    assert "sandbox user separation" in result.content


# ---- output handling -------------------------------------------------------


def test_extract_text_drops_script_and_style():
    html = (
        "<html><head><style>body{color:red}</style>"
        "<script>alert('x')</script></head>"
        "<body><h1>Title</h1><p>Hello &amp; welcome</p></body></html>"
    )
    text = extract_text(html)
    assert "Title" in text and "Hello & welcome" in text
    assert "alert" not in text and "color:red" not in text


def test_extract_text_survives_malformed_markup():
    assert "hi" in extract_text("<p>hi<<<>>")


def test_build_browser_tools_is_core_and_named():
    tools = build_browser_tools({"sandbox_user": "ares-sbx", "workdir": "/x"})
    assert [t.name for t in tools] == ["fetch_page"]
    assert tools[0].core is True


# ---- blocked / empty page detection ----------------------------------------

from ares.plugins.tools.browser_tools import _looks_blocked  # noqa: E402


def test_bot_wall_is_reported_as_blocked():
    """12 of 67 live fetches returned <200 chars and were reported ok=True."""
    assert _looks_blocked("")
    assert _looks_blocked("   \n  ")
    assert _looks_blocked("Just a moment... Checking your browser before accessing.")


def test_consent_and_captcha_walls_are_caught():
    filler = " lorem ipsum dolor sit amet consectetur adipiscing elit " * 12
    assert _looks_blocked("Please enable JavaScript to continue." + filler)
    assert _looks_blocked("Verify you are human before proceeding." + filler)


def test_a_real_article_is_not_blocked():
    article = (
        "Jakarta protests continued for a third day on Tuesday as demonstrators "
        "gathered near the National Monument. Police said 62 separate protests "
        "were held across the capital, with organisers calling for further "
        "action later in the week. Traffic around Monas was closed from dawn."
    )
    assert not _looks_blocked(article)
