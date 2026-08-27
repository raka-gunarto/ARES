from __future__ import annotations

import asyncio
import getpass
import os
import signal

from ares.core.tool import BaseTool, ToolContext, ToolResult
from ares.core.utils.logging import get_logger

logger = get_logger(__name__)

MAX_OUTPUT_CHARS = 4000

# The sole sudo entry point from the `ares` daemon to the `ares-sbx` sandbox
# user (spec §15). Installed OUTSIDE the app tree by provision.sh so it is not
# part of the self-edit surface; the runner scrubs the environment (env -i) and
# sets cwd, so the daemon passes neither through.
RUNNER_PATH = "/usr/local/sbin/ares-sbx-runner"

DEV_WARNING = (
    "[warning: sandbox_user not configured; running as the daemon user — DEV ONLY]\n"
)


class RunShell(BaseTool):
    """Run a shell command as an unprivileged sandbox user."""

    name = "run_shell"
    description = (
        "Run a shell command. This executes as an UNPRIVILEGED sandbox user with "
        "NO access to secrets or ARES's own live code. To install packages or "
        "change the system, use request_privilege. To change ARES's own code, "
        "use open_pr."
    )
    keywords = (
        "shell",
        "run",
        "command",
        "execute",
        "bash",
        "script",
        "terminal",
        "cli",
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_s": {"type": "integer"},
        },
        "required": ["command"],
    }
    core = True  # always in context (§5): promoted from discoverable-only

    def __init__(
        self,
        sandbox_user: str,
        workdir: str,
        timeout_default_s: int = 30,
        timeout_max_s: int = 120,
    ) -> None:
        """Store sandboxed-shell configuration."""
        self.sandbox_user = sandbox_user
        self.workdir = workdir
        self.timeout_default_s = timeout_default_s
        self.timeout_max_s = timeout_max_s

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute a shell command per spec §15 — never as the daemon's own uid in prod."""
        command = kwargs.get("command", "")
        if not command.strip():
            return ToolResult(False, "error: empty command")

        timeout_s = kwargs.get("timeout_s") or self.timeout_default_s
        if timeout_s > self.timeout_max_s:
            return ToolResult(False, f"error: timeout_s exceeds max {self.timeout_max_s}")

        env_mode = os.environ.get("ARES_ENV", "dev")

        # SECURITY: never run as the daemon's own uid in prod (§15).
        me = getpass.getuser()
        if env_mode == "prod" and (not self.sandbox_user or self.sandbox_user == me):
            logger.error("run_shell refused: would run as the daemon user in prod")
            return ToolResult(
                False, "error: shell execution refused (no sandbox user separation in prod)"
            )

        warning = ""
        if self.sandbox_user:
            # prod (§15): the sole sudo entry point is ares-sbx-runner, which
            # scrubs the environment (env -i) and sets cwd itself. Pass the
            # command as ONE argv element (never a daemon-side shell string),
            # and pass NEITHER env NOR cwd — the runner is the guarantee.
            argv = ["sudo", "-n", "-u", self.sandbox_user, RUNNER_PATH, command]
            run_env = None
            run_cwd = None
        else:
            # dev (sandbox_user=""): run directly with a restricted env (never
            # the ARES secret env, §14.3/§15) and a loud warning.
            argv = ["/bin/bash", "-lc", command]
            warning = DEV_WARNING
            run_env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": self.workdir or "/tmp",
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            }
            run_cwd = self.workdir or None

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=run_cwd,
                env=run_env,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            return ToolResult(False, f"error: failed to run shell: {e}")

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            return ToolResult(False, f"error: timed out after {timeout_s}s")

        rc = proc.returncode
        output = stdout.decode(errors="replace")
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n...truncated"
        output = warning + output

        if rc == 0:
            return ToolResult(True, output)
        return ToolResult(True, output + f"\n[exit code {rc}]")


def build_shell_tools(config: dict) -> list[BaseTool]:
    """Factory for the shell tool plugin, used by main.py."""
    return [
        RunShell(
            config.get("sandbox_user", ""),
            config.get("workdir", ""),
            config.get("timeout_default_s", 30),
            config.get("timeout_max_s", 120),
        )
    ]
