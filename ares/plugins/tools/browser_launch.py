"""Launch template and user-separation check for the stateful browser (§6.6).

Kept apart from the session so the security-relevant parts — which uid runs
the browser, and the exact argv it gets — can be read and tested on their own.
"""
from __future__ import annotations

import getpass
import os
import shlex

WINDOW_W, WINDOW_H = 1280, 900
# Headless Chromium announces itself ("HeadlessChrome" in the user agent,
# navigator.webdriver, an 800x600 screen inside the window), and Cloudflare-style
# bot checks never let such a browser through — ordering food on the operator's
# own account stalled at "Just a moment...". So both web tools present as the
# ordinary desktop Chromium they are: the same engine and major version (read
# from the installed binary at launch, so it never drifts), only without the
# headless/automation tells. It does not pretend to be another OS or browser,
# and an interactive challenge is still for the operator to solve (§17).
FALLBACK_MAJOR = 150
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/${{v:-{FALLBACK_MAJOR}}}.0.0.0 Safari/537.36"
)


def version_probe(binary: str) -> str:
    """Shell snippet setting `$v` to the binary's major version (empty on failure)."""
    return (
        f"v=$({shlex.quote(binary)} --version 2>/dev/null "
        "| grep -oE '[0-9]+[.][0-9.]+' | head -n1 | cut -d. -f1); "
    )


def presentation_flags() -> list[str]:
    """Flags that drop the headless tells; needs `version_probe` run first."""
    return [
        f"--window-size={WINDOW_W},{WINDOW_H}",
        shlex.quote(f"--screen-info={{{WINDOW_W}x{WINDOW_H}}}"),
        # Double quotes: $v must expand.
        f'"--user-agent={USER_AGENT}"',
        "--disable-blink-features=AutomationControlled",
    ]


def separation_error(browser_user: str, sandbox_user: str) -> str | None:
    """Why launching would break the §14 user separation (prod only), else None.

    The profile holds the operator's logged-in sessions, so it must be
    unreadable to the daemon uid AND to run_shell's sandbox uid.
    """
    if os.environ.get("ARES_ENV", "dev") != "prod":
        return None
    if not browser_user or browser_user == getpass.getuser():
        return "no dedicated browser user configured (browser_user)"
    if browser_user == sandbox_user:
        return "browser_user must differ from the run_shell sandbox user"
    return None


def build_launch_command(binary: str, profile_dir: str, proxy_port: int) -> str:
    """The fixed shell template the browser runner executes.

    Every variable part is shlex-quoted (the profile's `$HOME` prefix and the
    `$v` version read from the binary itself are the only expansions). DevTools
    rides the runner's stdin/stdout: fd 3 reads commands from our stdin, fd 4
    writes replies to our stdout.
    """
    if profile_dir.startswith("/"):
        prof = shlex.quote(profile_dir)
    else:
        prof = '"$HOME"/' + shlex.quote(profile_dir)
    flags = [
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",  # no user namespaces in the microVM; uid-isolated instead
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--mute-audio",
        *presentation_flags(),
        f"--proxy-server=http://127.0.0.1:{int(proxy_port)}",
        # Chromium never proxies localhost by default; "<-loopback>" removes that
        # bypass so loopback requests reach the proxy and get refused.
        shlex.quote("--proxy-bypass-list=<-loopback>"),
        # Nothing may route around the proxy.
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--remote-debugging-pipe",
    ]
    return (
        f"umask 077 && mkdir -p {prof} && "
        # Stale locks from a killed browser; the daemon is the only launcher.
        f"rm -f {prof}/SingletonLock {prof}/SingletonSocket {prof}/SingletonCookie; "
        + version_probe(binary)
        + f"exec {shlex.quote(binary)} " + " ".join(flags)
        + f" --user-data-dir={prof} about:blank"
        + " 3<&0 4>&1 0</dev/null 1>/dev/null 2>/dev/null"
    )
