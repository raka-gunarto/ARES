"""ARES update listener — spec §19.

TRUSTED ROOT COMPONENT, run as `ares-deploy`. Stdlib only. Never imports
`ares`. Keep this file tiny and auditable, mirroring `broker/aresbrokerd.py`:
a supply-chain issue in ARES's dependency tree can't reach a process that
never imports ARES's dependencies, and a small file is one a human can
actually read in full before trusting it with deploy privileges.

Merge-to-`main` is the human gate (spec §18): branch protection means a
human clicked "merge". This listener only ever deploys the configured
`branch` — it never pulls arbitrary refs — so the one thing this process is
trusted to do (restart the daemon with new code) only ever happens with a
human already in the loop upstream.

Two triggers, one serialised action:
  - Webhook: verifies GitHub's `X-Hub-Signature-256` HMAC before trusting
    any payload (constant-time compare — timing attacks on the signature
    would otherwise let an attacker forge a trigger without the secret).
  - Poll fallback: catches any missed/dropped webhook delivery.

Update action aborts (no swap, no restart) if the smoke import fails, so a
broken checkout can never take down the currently-running daemon.
"""
from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import logging
import os
import socketserver
import subprocess
import sys
import threading
import time

DEFAULT_CONFIG_PATH = "/etc/ares/updater.json"
LOG_PATH = "/var/log/ares-updater.log"

log = logging.getLogger("aresupdater")

# Serialises perform_update() so a webhook delivery and a poll tick can never
# race each other into concurrent checkouts/symlink swaps.
_update_lock = threading.Lock()


def _configure_logging() -> None:
    try:
        logging.basicConfig(
            filename=LOG_PATH,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )
    except OSError:
        logging.basicConfig(
            stream=sys.stderr,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )


def load_config(path: str | None = None) -> dict:
    path = path or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _remote_url(config: dict) -> str:
    return config.get("remote") or f"https://github.com/{config['repo']}.git"


def verify_signature(secret: bytes, body: bytes, signature_header: str) -> bool:
    """Verify GitHub's X-Hub-Signature-256 header over `body` with `secret`.

    Constant-time comparison (hmac.compare_digest) so a malicious sender
    can't learn the correct signature byte-by-byte via response timing.
    Never raises: any malformed input is simply "not verified".
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    try:
        digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    except TypeError:
        return False
    expected = "sha256=" + digest
    try:
        return hmac.compare_digest(expected, signature_header)
    except (TypeError, ValueError):
        return False


def remote_sha(remote_url: str, branch: str) -> str | None:
    """`git ls-remote <remote_url> refs/heads/<branch>`, parse the SHA."""
    try:
        proc = subprocess.run(
            ["git", "ls-remote", remote_url, f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("remote_sha: git ls-remote failed to run")
        return None
    if proc.returncode != 0:
        log.error("remote_sha: git ls-remote rc=%s stderr=%s", proc.returncode, proc.stderr)
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    parts = out.splitlines()[0].split()
    return parts[0] if parts else None


def deployed_sha(released_sha_file: str = "/opt/ares/RELEASED_SHA") -> str | None:
    """Read the SHA of the currently-deployed release, if recorded."""
    try:
        with open(released_sha_file, "r", encoding="utf-8") as f:
            sha = f.read().strip()
    except OSError:
        return None
    return sha or None


def smoke_import(venv_python: str, cwd: str) -> bool:
    """Run `import ares.core.agent` in the checked-out tree. This is the
    abort gate: a checkout that can't even import must never be swapped in.
    """
    try:
        proc = subprocess.run(
            [venv_python, "-c", "import ares.core.agent"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("smoke_import: failed to run venv python")
        return False
    if proc.returncode != 0:
        log.error(
            "smoke_import: rc=%s stderr=%s", proc.returncode, proc.stderr.strip()
        )
        return False
    return True


def checkout_release(config: dict, new_sha: str) -> str:
    """Fetch `branch` and check `new_sha` out into `releases_dir/<sha>`.

    Uses a persistent local clone (`src_dir`) plus `git worktree add` so
    each release is a cheap, independent checkout that can be kept around
    for rollback.
    """
    remote_url = _remote_url(config)
    branch = config["branch"]
    src_dir = config.get("src_dir", "/opt/ares/src")
    releases_dir = config.get("releases_dir", "/opt/ares/releases")
    release_path = os.path.join(releases_dir, new_sha)

    os.makedirs(releases_dir, exist_ok=True)
    if os.path.isdir(release_path):
        return release_path

    if not os.path.isdir(os.path.join(src_dir, ".git")):
        os.makedirs(os.path.dirname(src_dir) or "/", exist_ok=True)
        subprocess.run(["git", "clone", remote_url, src_dir], check=True)

    subprocess.run(["git", "-C", src_dir, "fetch", "origin", branch], check=True)
    subprocess.run(
        ["git", "-C", src_dir, "worktree", "add", "--detach", release_path, new_sha],
        check=True,
    )
    return release_path


def swap_symlink(app_dir: str, release_path: str) -> None:
    """Atomically repoint the `app` symlink at the new release.

    Writes a temp symlink then `os.replace`s it over the live one — a
    single filesystem rename, so a reader of `app_dir` never observes a
    half-updated link. The previous release directory is left on disk
    untouched for rollback.
    """
    tmp_link = f"{app_dir}.tmp-{os.getpid()}"
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(release_path, tmp_link)
    os.replace(tmp_link, app_dir)


def run_restart(config: dict) -> bool:
    """Run the one fixed, sudoers-approved restart command. Never shell=True,
    never built from anything but the config's own fixed argv list."""
    try:
        proc = subprocess.run(config["restart_cmd"], check=False)
    except OSError:
        log.exception("run_restart: failed to execute restart_cmd")
        return False
    if proc.returncode != 0:
        log.error("run_restart: restart_cmd exited rc=%s", proc.returncode)
        return False
    return True


def write_released_sha(config: dict, new_sha: str) -> None:
    path = config.get("released_sha_file", "/opt/ares/RELEASED_SHA")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_sha + "\n")


def perform_update(config: dict, new_sha: str) -> bool:
    """Serialised update action per spec §19 steps 1-5.

    Sequence: checkout -> smoke import (abort gate) -> swap symlink ->
    restart -> record deployed SHA. Any exception is caught and logged;
    this function must never crash its caller (webhook handler or poll
    loop) and must never leave a partially-applied release live without
    at least attempting the restart/record steps.
    """
    with _update_lock:
        try:
            release_path = checkout_release(config, new_sha)
        except Exception:
            log.exception("perform_update: checkout_release failed for %s", new_sha)
            return False

        venv_python = config.get("venv_python", "/opt/ares/venv/bin/python")
        if not smoke_import(venv_python, release_path):
            log.error(
                "perform_update: smoke import failed for %s — aborting, "
                "running daemon left untouched",
                new_sha,
            )
            return False

        try:
            swap_symlink(config["app_dir"], release_path)
            run_restart(config)
            write_released_sha(config, new_sha)
        except Exception:
            log.exception(
                "perform_update: post-smoke step failed for %s", new_sha
            )
            return False

        log.info("perform_update: deployed %s", new_sha)
        return True


class _WebhookServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_cls, config: dict) -> None:
        self.config = config
        super().__init__(server_address, handler_cls)


class WebhookHandler(http.server.BaseHTTPRequestHandler):
    """Handles `POST /gh` GitHub webhook deliveries. `self.server.config`
    is set by `_WebhookServer`, which is how config/secret reach the
    handler without any module-level global state."""

    server: _WebhookServer

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _reject(self, code: int) -> None:
        self.send_response(code)
        self.end_headers()

    def do_POST(self) -> None:
        config = self.server.config
        if self.path != "/gh":
            self._reject(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._reject(400)
            return
        body = self.rfile.read(length) if length > 0 else b""

        secret_env = config.get("webhook_secret_env", "ARES_WEBHOOK_SECRET")
        secret = os.environ.get(secret_env, "")
        signature = self.headers.get("X-Hub-Signature-256", "")

        if not secret or not verify_signature(secret.encode("utf-8"), body, signature):
            log.warning("webhook: signature verification failed")
            self._reject(401)
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reject(400)
            return

        branch = config.get("branch", "main")
        if payload.get("ref") != f"refs/heads/{branch}":
            # Verified push, but not to the deployed branch: nothing to do.
            self.send_response(200)
            self.end_headers()
            return

        new_sha = payload.get("after") or remote_sha(_remote_url(config), branch)
        if not new_sha:
            self._reject(400)
            return

        log.info("webhook: triggering update to %s", new_sha)
        threading.Thread(
            target=perform_update, args=(config, new_sha), daemon=True
        ).start()
        self.send_response(202)
        self.end_headers()


def poll_loop(config: dict) -> None:
    """Fallback for missed webhook deliveries: compare remote vs deployed
    SHA every `poll_seconds` and trigger an update on drift."""
    branch = config.get("branch", "main")
    remote_url = _remote_url(config)
    released_sha_file = config.get("released_sha_file", "/opt/ares/RELEASED_SHA")
    poll_seconds = config.get("poll_seconds", 300)

    while True:
        try:
            remote = remote_sha(remote_url, branch)
            if remote and remote != deployed_sha(released_sha_file):
                log.info("poll_loop: remote %s differs from deployed, updating", remote)
                perform_update(config, remote)
        except Exception:
            log.exception("poll_loop: error in iteration")
        time.sleep(poll_seconds)


def main() -> None:
    _configure_logging()
    config = load_config()
    log.info(
        "aresupdater starting: repo=%s branch=%s", config.get("repo"), config.get("branch")
    )

    poll_thread = threading.Thread(target=poll_loop, args=(config,), daemon=True)
    poll_thread.start()

    port = config.get("webhook_port", 8790)
    server = _WebhookServer(("0.0.0.0", port), WebhookHandler, config)
    log.info("aresupdater webhook listening on :%s/gh", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
