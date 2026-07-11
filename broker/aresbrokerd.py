"""ARES root broker daemon — spec §16.3.

TRUSTED ROOT COMPONENT. Stdlib only. Never imports `ares`. Keep this file
tiny and auditable (< 200 lines). It polls `priv_requests` for rows that a
human operator has approved via the dashboard, re-validates them against a
regex allowlist (defence in depth against operator mis-approval), builds a
FIXED argv from the request kind (never invokes a shell, never splits
attacker-controlled text), and executes them.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

DEFAULT_CONFIG_PATH = "/etc/ares/broker.json"
LOG_PATH = "/var/log/ares-broker.log"
MAX_OUTPUT = 8000

log = logging.getLogger("aresbrokerd")


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


def validate(kind: str, command: str, allow: dict) -> bool:
    patterns = allow.get(kind, [])
    return any(re.fullmatch(p, command) for p in patterns)


def build_argv(kind: str, command: str) -> list[str] | None:
    if kind == "package_install":
        if re.fullmatch(r"^[a-z0-9][a-z0-9+.\-]*$", command):
            return ["apt-get", "install", "-y", command]
        return None
    if kind == "service_action":
        m = re.fullmatch(r"^(restart|status) (ares|ares-updater)$", command)
        if m:
            return ["systemctl", m.group(1), m.group(2)]
        return None
    if kind == "command":
        return None
    return None


def run_command(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output[:MAX_OUTPUT]
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 1, str(e)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_approved(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, user_id, kind, command, reason, status FROM priv_requests "
            "WHERE status='approved'"
        )
        return cur.fetchall()
    finally:
        conn.close()


def mark_executing(db_path: str, req_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE priv_requests SET status='executing' WHERE id=? AND status='approved'",
            (req_id,),
        )
        conn.commit()
    finally:
        conn.close()


def mark_done(db_path: str, req_id: str, exit_code: int, output: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE priv_requests SET status='done', exit_code=?, output=?, "
            "executed_at=? WHERE id=?",
            (exit_code, output, _now(), req_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(
    db_path: str, req_id: str, output: str, exit_code: int | None = None
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE priv_requests SET status='failed', exit_code=?, output=?, "
            "executed_at=? WHERE id=?",
            (exit_code, output, _now(), req_id),
        )
        conn.commit()
    finally:
        conn.close()


def process_once(config: dict) -> None:
    db_path = config["db_path"]
    allow = config.get("allow", {})
    for row in select_approved(db_path):
        req_id, kind, command = row["id"], row["kind"], row["command"]

        if not validate(kind, command, allow):
            log.info("rejecting %s (%s): not allowlisted", req_id, kind)
            mark_failed(db_path, req_id, output="rejected: not allowlisted")
            continue

        argv = build_argv(kind, command)
        if argv is None:
            log.info("rejecting %s (%s): no argv built", req_id, kind)
            mark_failed(db_path, req_id, output="rejected: not allowlisted")
            continue

        log.info("executing %s: %r", req_id, argv)
        mark_executing(db_path, req_id)
        rc, out = run_command(argv)
        if rc == 0:
            log.info("done %s: rc=0", req_id)
            mark_done(db_path, req_id, rc, out)
        else:
            log.info("failed %s: rc=%s", req_id, rc)
            mark_failed(db_path, req_id, out, rc)


def main() -> None:
    _configure_logging()
    config = load_config()
    log.info("aresbrokerd starting, db=%s", config.get("db_path"))
    while True:
        try:
            process_once(config)
        except Exception:
            log.exception("error in broker loop iteration")
        time.sleep(config.get("poll_seconds", 5))


if __name__ == "__main__":
    main()
