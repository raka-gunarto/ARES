from __future__ import annotations

from ares.core.tool import BaseTool, ToolContext, ToolResult


class RequestPrivilege(BaseTool):
    """File a privilege request for installation, service action, or command."""

    name = "request_privilege"
    description = (
        "File a privilege request for sudo access, package installation, or system commands. "
        "Requires approval from an administrator."
    )
    keywords = ("sudo", "install", "package", "privilege", "permission", "system", "apt", "root", "admin")
    parameters = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["package_install", "service_action", "command"],
                "description": "Type of privilege request",
            },
            "command": {
                "type": "string",
                "description": (
                    "The package or command to request. For package_install this "
                    "is exactly ONE lowercase package name (e.g. 'chromium') — "
                    "never a list, and never flags. Need several packages? File "
                    "one request per package."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Reason for the request",
            },
        },
        "required": ["kind", "command", "reason"],
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """File a privilege request."""
        try:
            svc = ctx.services.get("privileges")
            if svc is None:
                return ToolResult(False, "Privilege queue is not configured.")

            req = await svc.create(
                ctx.user_id, kwargs["kind"], kwargs["command"], kwargs["reason"]
            )
            return ToolResult(True, f"Request {req.id} filed; awaiting approval.")
        except Exception as e:
            return ToolResult(False, f"error: {e}")


class GetPrivilegeRequests(BaseTool):
    """List privilege requests for this user."""

    name = "get_privilege_requests"
    description = "List your privilege requests with their current status."
    keywords = ("privilege", "requests", "pending", "approved", "sudo", "status")
    parameters = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter by status (pending, approved, denied, executing, done, failed)",
            }
        },
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """List privilege requests for this user."""
        try:
            svc = ctx.services.get("privileges")
            if svc is None:
                return ToolResult(False, "Privilege queue is not configured.")

            reqs = await svc.list(kwargs.get("status"))

            # Filter to this user's requests only
            user_reqs = [r for r in reqs if r.user_id == ctx.user_id]

            if not user_reqs:
                return ToolResult(True, "No privilege requests.")

            lines = []
            for r in user_reqs:
                lines.append(f"{r.id} | {r.kind} | {r.status} | {r.command}")

            content = "\n".join(lines)
            return ToolResult(True, content)
        except Exception as e:
            return ToolResult(False, f"error: {e}")


PRIV_TOOLS: list[BaseTool] = [RequestPrivilege(), GetPrivilegeRequests()]
