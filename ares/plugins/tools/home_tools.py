from __future__ import annotations

import typing

from ares.core.tool import BaseTool, ToolContext, ToolResult

if typing.TYPE_CHECKING:
    pass


class GetHomeState(BaseTool):
    """Get Home Assistant entity state or domain summary."""

    name = "get_home_state"
    description = (
        "Get the current state of a Home Assistant entity, all entities in a domain, "
        "or a filtered summary of all devices. "
        "Provide entity_id to check a single device, domain to list all devices in that domain, "
        "or neither for a summary snapshot."
    )
    keywords = ("home", "state", "status", "temperature", "light", "door", "sensor", "house")
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "The entity ID to check (e.g., 'light.kitchen')",
            },
            "domain": {
                "type": "string",
                "description": "The domain to list (e.g., 'light', 'climate', 'sensor')",
            },
        },
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute get_home_state."""
        svc = ctx.services.get("home_assistant")
        if svc is None:
            return ToolResult(False, "Home Assistant is not configured.")

        entity_id = kwargs.get("entity_id")
        domain = kwargs.get("domain")

        try:
            if entity_id:
                # Get single entity state
                state_obj = await svc.get_state(entity_id)
                if state_obj is None:
                    return ToolResult(True, f"Entity {entity_id} not found.")

                # Format state with key attributes
                state_str = state_obj.get("state", "unknown")
                attrs = state_obj.get("attributes", {})
                lines = [f"{entity_id}: {state_str}"]

                # Add key attributes
                for key in ["friendly_name", "temperature", "humidity", "brightness"]:
                    if key in attrs:
                        lines.append(f"  {key}: {attrs[key]}")

                return ToolResult(True, "\n".join(lines))

            elif domain:
                # Get all entities in domain, capped at 50 lines
                states = await svc.get_states(domain)
                lines = []
                for state_obj in states[:50]:
                    eid = state_obj.get("entity_id", "unknown")
                    state = state_obj.get("state", "unknown")
                    lines.append(f"{eid}: {state}")

                content = "\n".join(lines)
                if len(states) > 50:
                    content += f"\n... ({len(states) - 50} more entities in {domain})"

                return ToolResult(True, content)

            else:
                # No args: get snapshot summary
                summary = await svc.snapshot_summary()
                return ToolResult(True, summary)

        except Exception as e:
            return ToolResult(False, f"Home error: {e}")


class ControlDevice(BaseTool):
    """Control a Home Assistant device via service call."""

    name = "control_device"
    description = (
        "Send a control command to a Home Assistant device. "
        "Maps action names like 'turn_on', 'turn_off', 'set_temperature' to HA service calls. "
        "Optionally provide a value for actions that accept one."
    )
    keywords = (
        "home",
        "control",
        "turn",
        "switch",
        "light",
        "set",
        "heating",
        "lock",
        "open",
        "close",
    )
    parameters = {
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "The entity ID to control (e.g., 'light.kitchen')",
            },
            "action": {
                "type": "string",
                "description": "The action to perform (e.g., 'turn_on', 'turn_off', 'set_temperature')",
            },
            "value": {
                "type": "string",
                "description": "Optional value for the action (e.g., '21' for set_temperature)",
            },
        },
        "required": ["entity_id", "action"],
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute control_device."""
        svc = ctx.services.get("home_assistant")
        if svc is None:
            return ToolResult(False, "Home Assistant is not configured.")

        entity_id = kwargs["entity_id"]
        action = kwargs["action"]
        value = kwargs.get("value")

        try:
            # Extract domain from entity_id
            domain = entity_id.split(".")[0]

            # SECURITY: refuse safety-gated outbound actions (doors/alarm), which
            # must never be triggered by retrieved/external content (§14, C3).
            if not svc.control_allowed(domain, action):
                return ToolResult(
                    False,
                    f"Refused: '{action}' on '{entity_id}' is a safety-gated action "
                    "(e.g. unlocking a door or disarming the alarm) and cannot be "
                    "triggered by ARES. The operator can do it from the dashboard.",
                )

            # Build data dict based on action and value
            data: dict[str, typing.Any] = {}
            if value is not None:
                if action == "set_temperature":
                    data["temperature"] = value
                else:
                    data["value"] = value

            # Call the service
            result = await svc.call_service(domain, action, entity_id, data)

            return ToolResult(True, f"{action} sent to {entity_id}. Result: {result}")

        except Exception as e:
            return ToolResult(False, f"Home error: {e}")


class ListDevices(BaseTool):
    """List Home Assistant devices with friendly names."""

    name = "list_devices"
    description = (
        "List all Home Assistant devices, optionally filtered by domain. "
        "Shows entity ID and friendly name for each device. "
        "If no domain is specified, lists devices across all domains."
    )
    keywords = ("home", "devices", "entities", "list", "available")
    parameters = {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Optional domain to filter by (e.g., 'light', 'climate', 'sensor')",
            },
        },
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute list_devices."""
        svc = ctx.services.get("home_assistant")
        if svc is None:
            return ToolResult(False, "Home Assistant is not configured.")

        domain = kwargs.get("domain")

        try:
            # Get states for domain or all
            states = await svc.get_states(domain) if domain else await svc.get_states(None)

            lines = []
            for state_obj in states[:80]:
                entity_id = state_obj.get("entity_id", "unknown")
                attrs = state_obj.get("attributes", {})
                friendly_name = attrs.get("friendly_name", entity_id)
                lines.append(f"{entity_id} — {friendly_name}")

            content = "\n".join(lines)
            if len(states) > 80:
                content += f"\n... ({len(states) - 80} more entities)"

            return ToolResult(True, content)

        except Exception as e:
            return ToolResult(False, f"Home error: {e}")


class CameraSnapshot(BaseTool):
    """Fetch a snapshot from a Home Assistant camera."""

    name = "camera_snapshot"
    description = (
        "Fetch a live snapshot from a Home Assistant camera. "
        "The snapshot is saved to a temporary file and the path is returned. "
        "v1: no vision analysis — the snapshot path is provided for manual use."
    )
    keywords = ("camera", "snapshot", "look", "see", "check", "picture", "view")
    parameters = {
        "type": "object",
        "properties": {
            "camera_entity": {
                "type": "string",
                "description": "The camera entity ID (e.g., 'camera.front_door')",
            },
        },
        "required": ["camera_entity"],
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute camera_snapshot."""
        svc = ctx.services.get("home_assistant")
        if svc is None:
            return ToolResult(False, "Home Assistant is not configured.")

        camera_entity = kwargs["camera_entity"]

        try:
            path = await svc.camera_snapshot(camera_entity)
            size = path.stat().st_size
            return ToolResult(True, f"Snapshot saved: {path} ({size} bytes)")

        except Exception as e:
            return ToolResult(False, f"Home error: {e}")


HOME_TOOLS: list[BaseTool] = [
    GetHomeState(),
    ControlDevice(),
    ListDevices(),
    CameraSnapshot(),
]
