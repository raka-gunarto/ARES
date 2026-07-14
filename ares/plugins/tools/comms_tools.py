from __future__ import annotations

from ares.core.channel import ChannelType
from ares.core.tool import BaseTool, ToolContext, ToolResult


class PlaceCall(BaseTool):
    """Dial the user's configured SIP URI and speak a message."""

    name = "place_call"
    description = (
        "Dial the user's configured SIP URI, speak a message via TTS, then listen for their response. "
        "Returns immediately; the user's reply arrives later as a new event."
    )
    keywords = ("call", "phone", "ring", "dial", "urgent", "reach")
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message to speak to the user."}
        },
        "required": ["message"]
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Initiate an outgoing call with speech."""
        svc = ctx.services.get("sip")
        if svc is None:
            return ToolResult(False, "SIP is not configured.")

        uri = svc.user_uris.get(ctx.user_id)
        if not uri:
            return ToolResult(False, "No SIP address configured for this user.")

        try:
            await svc.call_and_speak(uri, kwargs["message"], True)
        except Exception as e:
            return ToolResult(False, f"SIP error: {e}")

        # We are now on a call with the user: route further `speak` output into
        # the live call. Without this the active channel stays whatever the
        # request arrived on (e.g. the dashboard), so anything said after the
        # opening message is delivered there and the caller hears only silence.
        ctx.session.active_channel = ChannelType.SIP_CALL
        return ToolResult(True, "Call initiated.")


class SendSipMessage(BaseTool):
    """Send a text message via SIP MESSAGE."""

    name = "send_sip_message"
    description = (
        "Send a text message to the user via SIP MESSAGE protocol. "
        "Useful for asynchronous communication via their SIP messaging interface."
    )
    keywords = ("sip", "text", "message", "send", "sms")
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message text to send."}
        },
        "required": ["message"]
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Send a SIP text message."""
        svc = ctx.services.get("sip")
        if svc is None:
            return ToolResult(False, "SIP is not configured.")

        uri = svc.user_uris.get(ctx.user_id)
        if not uri:
            return ToolResult(False, "No SIP address configured for this user.")

        try:
            ok = await svc.send_message(uri, kwargs["message"])
            if ok:
                return ToolResult(True, "Message sent.")
            else:
                return ToolResult(False, "Message failed to send.")
        except Exception as e:
            return ToolResult(False, f"SIP error: {e}")


COMMS_TOOLS: list[BaseTool] = [PlaceCall(), SendSipMessage()]
