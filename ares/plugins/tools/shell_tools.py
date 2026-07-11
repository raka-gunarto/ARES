from __future__ import annotations

import asyncio
import getpass
import os
import signal

from ares.core.tool import BaseTool, ToolContext, ToolResult
from ares.core.utils.logging import get_logger

logger = get_logger(__name__)

MAX_OUTPUT_CHARS = 4000

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
    core = False

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

        # Restricted env — never the ARES secret env (§14.3/§15).
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": self.workdir or "/tmp",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }

        warning = ""
        if self.sandbox_user:
            argv = ["sudo", "-n", "-u", self.sandbox_user, "/bin/bash", "-lc", command]
        else:
            argv = ["/bin/bash", "-lc", command]
            warning = DEV_WARNING

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=(self.workdir or None),
                env=env,
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
