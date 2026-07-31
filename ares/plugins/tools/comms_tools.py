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


class EndCall(BaseTool):
    """Hang up the call that is currently in progress."""

    name = "end_call"
    description = (
        "Hang up the call currently in progress. Use this when the conversation is "
        "finished, when the caller says goodbye, or when they ask you to hang up. "
        "Pass `farewell` to say a closing line first — it is spoken in full before "
        "the line drops. Anything you say after this tool will NOT reach the caller."
    )
    keywords = ("hang up", "hangup", "end call", "goodbye", "bye", "disconnect")
    parameters = {
        "type": "object",
        "properties": {
            "farewell": {
                "type": "string",
                "description": "Optional closing line spoken before hanging up.",
            }
        },
        "required": []
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Speak an optional farewell, then hang up."""
        svc = ctx.services.get("sip")
        if svc is None:
            return ToolResult(False, "SIP is not configured.")
        if not svc.has_active_call():
            return ToolResult(False, "There is no call in progress.")

        farewell = (kwargs.get("farewell") or "").strip()
        try:
            if farewell:
                # Blocks until playback finishes, so the hangup below cannot cut
                # the closing line off mid-word.
                await svc.speak_into_call(farewell)
            ended = await svc.hangup()
        except Exception as e:
            return ToolResult(False, f"SIP error: {e}")

        # The call is gone, so the router must stop aiming at it — otherwise the
        # final assistant turn fails over to PUSH and the caller gets a phone
        # notification of the goodbye they just heard.
        ctx.session.active_channel = ChannelType.CONSOLE

        if not ended:
            return ToolResult(False, "The call had already ended.")
        return ToolResult(True, "Call ended.")


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


COMMS_TOOLS: list[BaseTool] = [PlaceCall(), EndCall(), SendSipMessage()]
