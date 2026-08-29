from __future__ import annotations

import typing

import httpx

from ares.core.tool import BaseTool, ToolContext, ToolResult

if typing.TYPE_CHECKING:
    pass


def _describe_error(e: Exception) -> str:
    """Render an exception for the model, never as an empty string.

    httpx's timeout/transport exceptions carry an empty `str()`, which used to
    surface as a bare "Home error: " — the model could not tell a timeout from a
    refused connection, so it retried blindly. A timed-out service call is the
    dangerous case: HA may well have applied it, so say so and point at the
    check rather than inviting a repeat.
    """
    if isinstance(e, httpx.TimeoutException):
        return (
            "timed out waiting for Home Assistant. The command may still have "
            "been applied — check the entity with get_home_state before sending "
            "it again."
        )
    text = str(e).strip()
    return text or f"{type(e).__name__} (no detail)"


def _fmt_attr_value(value: typing.Any, limit: int = 200) -> str:
    """Render one HA attribute value, truncating runaway lists/strings so a
    single noisy entity (e.g. a light's `effect_list`) can't flood the reply."""
    s = str(value)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


# Home Assistant services take NAMED parameters, and the name differs per
# service. The old code mapped a bare `value` to a literal `"value"` field for
# everything except set_temperature — a field almost no HA service accepts — so
# every `set_hvac_mode(value="off")` was a guaranteed 400 Bad Request. The live
# trace shows the model then brute-forcing variants (`value`, `{"hvac_mode":..}`
# nested under value, `data` and `value` together), burning a cycle each time.
# Mapping the convenience field to the service's real parameter removes the
# whole class of failure.
_SERVICE_VALUE_FIELD: dict[str, str] = {
    "set_temperature": "temperature",
    "set_hvac_mode": "hvac_mode",
    "set_fan_mode": "fan_mode",
    "set_preset_mode": "preset_mode",
    "set_swing_mode": "swing_mode",
    "set_humidity": "humidity",
    "set_percentage": "percentage",
    "set_direction": "direction",
    "set_cover_position": "position",
    "set_cover_tilt_position": "tilt_position",
    "select_option": "option",
    "select_source": "source",
    "volume_set": "volume_level",
    "set_value": "value",  # number/input_number genuinely takes `value`
}


def _entity_attrs(state_obj: object) -> dict:
    """Attributes of a state payload, tolerating None/malformed responses."""
    if not isinstance(state_obj, dict):
        return {}
    attrs = state_obj.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _climate_off_hint(attrs: dict) -> str:
    """Suggest the working way to switch a climate entity off.

    Grounded in the live trace: this household's AC reports
    `supported_features: 9` — no `turn_off` service at all, so every
    `climate.turn_off` returned HTTP 500. When `hvac_modes` lists `off`, name
    the call that actually works instead of leaving the model to guess.
    """
    modes = attrs.get("hvac_modes")
    if isinstance(modes, list) and "off" in modes:
        return (
            f" This entity's hvac_modes are {modes}; switch it off with "
            "action='set_hvac_mode', data={'hvac_mode':'off'} — it has no "
            "working turn_off service."
        )
    return ""


class GetHomeState(BaseTool):
    """Get Home Assistant entity state or domain summary."""

    name = "get_home_state"
    description = (
        "Get the current state of a Home Assistant entity, all entities in a domain, "
        "or a filtered summary of all devices. "
        "Provide entity_id to check a single device (returns ALL of its attributes — "
        "e.g. a climate entity's hvac_modes, hvac_action, current_temperature, "
        "fan_modes and supported_features, which you need to decide how to control it), "
        "domain to list all devices in that domain (state only, capped at 50), "
        "or neither for a summary snapshot. "
        "Pass `attributes` to choose exactly which attribute keys to show (works for the "
        "domain listing too), or ['*'] for every attribute."
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
            "attributes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional attribute keys to show for each entity (e.g. "
                    "['hvac_modes','current_temperature']). Use ['*'] for every "
                    "attribute. Omit: a single entity shows all attributes, a domain "
                    "listing shows state only."
                ),
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
        override = kwargs.get("attributes")

        def keys_for(attrs: dict, default_all: bool) -> list:
            """Attribute keys to show: the caller's override (['*'] = all), else
            every attribute for a single entity / none for a domain listing."""
            if override:
                if "*" in override:
                    return list(attrs.keys())
                return [str(k) for k in override]
            return list(attrs.keys()) if default_all else []

        try:
            if entity_id:
                # Single entity: surface ALL attributes by default so the model can
                # see what actions are valid (hvac_modes, supported_features, ...).
                state_obj = await svc.get_state(entity_id)
                if state_obj is None:
                    return ToolResult(True, f"Entity {entity_id} not found.")

                state_str = state_obj.get("state", "unknown")
                attrs = state_obj.get("attributes", {})
                lines = [f"{entity_id}: {state_str}"]
                for key in keys_for(attrs, default_all=True):
                    if key in attrs:
                        lines.append(f"  {key}: {_fmt_attr_value(attrs[key])}")

                return ToolResult(True, "\n".join(lines))

            elif domain:
                # Domain listing stays compact (state only) unless attributes are
                # explicitly requested, to respect the 50-line cap.
                states = await svc.get_states(domain)
                lines = []
                for state_obj in states[:50]:
                    eid = state_obj.get("entity_id", "unknown")
                    state = state_obj.get("state", "unknown")
                    attrs = state_obj.get("attributes", {})
                    parts = [
                        f"{k}={_fmt_attr_value(attrs[k])}"
                        for k in keys_for(attrs, default_all=False)
                        if k in attrs
                    ]
                    suffix = f" [{', '.join(parts)}]" if parts else ""
                    lines.append(f"{eid}: {state}{suffix}")

                content = "\n".join(lines)
                if len(states) > 50:
                    content += f"\n... ({len(states) - 50} more entities in {domain})"

                return ToolResult(True, content)

            else:
                # No args: get snapshot summary
                summary = await svc.snapshot_summary()
                return ToolResult(True, summary)

        except Exception as e:
            return ToolResult(False, f"Home error: {_describe_error(e)}")


class ControlDevice(BaseTool):
    """Control a Home Assistant device via service call."""

    name = "control_device"
    description = (
        "Send a control command to a Home Assistant device: `action` is the HA "
        "service to call (e.g. 'turn_on', 'set_hvac_mode', 'set_fan_mode'). "
        "HA services take NAMED parameters — pass them in `data` (e.g. "
        "set_hvac_mode needs data={'hvac_mode':'off'}, set_fan_mode needs "
        "data={'fan_mode':'auto'}, a light dim needs data={'brightness_pct':40}). "
        "Not every device has a 'turn_off' service: if turn_off fails or the entity "
        "lacks it (check supported_features / hvac_modes via get_home_state), turn a "
        "climate device off with action='set_hvac_mode', data={'hvac_mode':'off'}. "
        "`value` is a shorthand accepted only for single-parameter services "
        "(set_temperature, set_hvac_mode, set_fan_mode, ...); anything else must "
        "use `data`."
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
                "description": "The HA service to call (e.g., 'turn_on', 'set_hvac_mode', 'set_temperature')",
            },
            "data": {
                "type": "object",
                "description": (
                    "Named service parameters passed straight to Home Assistant "
                    "(e.g. {'hvac_mode':'off'}, {'temperature':21}, "
                    "{'brightness_pct':40}). This is how most services take input."
                ),
            },
            "value": {
                "type": "string",
                "description": (
                    "Shorthand for a single-parameter service (e.g. '21' for "
                    "set_temperature, 'off' for set_hvac_mode). Prefer `data`; "
                    "services that take more than one field reject `value`."
                ),
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

            # Named service params come through `data`; `value` is a back-compat
            # convenience mapped to the service's REAL parameter name. Sending a
            # literal "value" field (the old fallback) is a guaranteed 400 for
            # every service except number.set_value, so an unmapped service is
            # refused with instructions rather than dispatched to fail.
            data: dict[str, typing.Any] = dict(kwargs.get("data") or {})
            if value is not None:
                field = _SERVICE_VALUE_FIELD.get(action)
                if field is None:
                    return ToolResult(
                        False,
                        f"'{action}' has no single-value form, so `value` cannot be "
                        f"mapped to a Home Assistant parameter. Pass the named "
                        f"field(s) in `data` instead, e.g. "
                        f"data={{'hvac_mode':'off'}}. Call get_home_state on "
                        f"{entity_id} to see which attributes it accepts.",
                    )
                data.setdefault(field, value)

            # Call the service
            result = await svc.call_service(domain, action, entity_id, data)

            # Report the resulting state so the caller can confirm it took effect,
            # rather than dumping HA's raw changed-entities array.
            new_state = None
            if isinstance(result, list):
                for s in result:
                    if isinstance(s, dict) and s.get("entity_id") == entity_id:
                        new_state = s.get("state")
                        break
            if new_state is not None:
                return ToolResult(True, f"{action} sent to {entity_id}; state is now '{new_state}'.")
            return ToolResult(True, f"{action} sent to {entity_id}.")

        except Exception as e:
            # A bare failure leaves the model unable to tell "nothing happened"
            # from "it may have applied" — the trace shows it retrying blindly
            # either way. Read the entity back so the report names the entity's
            # ACTUAL state, and for a climate turn_on/turn_off (HTTP 500 on
            # hardware that has no such service) name the call that does work.
            detail = _describe_error(e)
            suffix = ""
            try:
                after = await svc.get_state(entity_id)
                attrs = _entity_attrs(after)
                if isinstance(after, dict) and after.get("state") is not None:
                    suffix = f" {entity_id} is currently '{after.get('state')}'."
                if domain == "climate" and action in ("turn_on", "turn_off"):
                    suffix += _climate_off_hint(attrs)
            except Exception:
                # The read-back is best-effort; never mask the original failure.
                suffix = " (could not read the entity back to confirm.)"
            return ToolResult(False, f"Home error: {detail}{suffix}")


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
            return ToolResult(False, f"Home error: {_describe_error(e)}")


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
            return ToolResult(False, f"Home error: {_describe_error(e)}")


HOME_TOOLS: list[BaseTool] = [
    GetHomeState(),
    ControlDevice(),
    ListDevices(),
    CameraSnapshot(),
]
