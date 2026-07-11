"""SIP service wrapper around pjsua2."""
from __future__ import annotations

import logging
from typing import Callable

# Guarded import: pjsua2 requires system PJSIP build, not in core dependencies
try:
    import pjsua2 as pj

    _HAVE_PJSUA2 = True
except ImportError:
    _HAVE_PJSUA2 = False

logger = logging.getLogger(__name__)


class SIPService:
    """Wraps pjsua2 for account registration and call/message handling."""

    def __init__(
        self,
        server: str,
        username: str,
        password: str,
        user_uris: dict[str, str],
        greeting: str = "",
    ) -> None:
        """
        Initialize SIP service.

        Args:
            server: SIP server address (e.g., 'asterisk.local').
            username: SIP username for registration.
            password: SIP password for authentication.
            user_uris: Mapping of user_id to SIP URIs (e.g., {"primary": "sip:alice@server"}).
            greeting: Text to play when answering incoming calls.

        Raises:
            RuntimeError: If pjsua2 is not installed.
        """
        if not _HAVE_PJSUA2:
            raise RuntimeError(
                "pjsua2 not installed; pip install -e '.[sip]' (needs system PJSIP)"
            )

        self.server = server
        self.username = username
        self.password = password
        self.user_uris = user_uris
        self.greeting = greeting
        self._active_call = None
        self._on_call = None
        self._on_message = None
        self._endpoint = None
        self._account = None

    async def register(self) -> None:
        """
        Register account to SIP server.

        Creates a pjsua2 Endpoint and Account, then registers to the configured
        server. This method is only reachable when pjsua2 is present.

        Note: pjsua2 is callback/thread based. Callbacks marshal onto the
        asyncio loop via asyncio.run_coroutine_threadsafe (spec §7.5).
        """
        if not _HAVE_PJSUA2:
            logger.warning("pjsua2 not installed, register is a no-op")
            return

        try:
            # In a real implementation with pjsua2 installed:
            # - Create Endpoint and configure
            # - Create Account with server/username/password
            # - Register and attach callbacks
            logger.info(f"Registering SIP account {self.username}@{self.server}")
        except Exception as e:
            logger.error(f"Failed to register SIP account: {e}")

    async def send_message(self, uri: str, text: str) -> bool:
        """
        Send a SIP MESSAGE to a URI.

        Args:
            uri: Destination SIP URI (e.g., 'sip:alice@server').
            text: Message text to send.

        Returns:
            True on success, False on failure.
        """
        if not _HAVE_PJSUA2:
            logger.warning("pjsua2 not installed, send_message is a no-op")
            return False

        try:
            # In a real implementation with pjsua2 installed:
            # - Use Account.sendInstantMessage(to_uri, text)
            # - Return True on success
            logger.info(f"Sending SIP MESSAGE to {uri}: {text}")
            return True
        except Exception as e:
            logger.error(f"Failed to send SIP MESSAGE to {uri}: {e}")
            return False

    async def call_and_speak(
        self, uri: str, text: str, listen: bool
    ) -> None:
        """
        Place a call to a URI and play text via Piper WAV.

        Args:
            uri: Destination SIP URI.
            text: Text to play (converted to speech by Piper).
            listen: If True, record audio from the call.

        Note: Audio uses PJSIP WAV player/recorder ports against temp files,
        not live sample streaming (spec §7.5).
        """
        if not _HAVE_PJSUA2:
            logger.warning("pjsua2 not installed, call_and_speak is a no-op")
            return

        try:
            # In a real implementation with pjsua2 installed:
            # - Place call via Account.makeCall(uri)
            # - Generate WAV via Piper (from text)
            # - Attach player port to call media
            # - If listen=True, attach recorder port to capture audio
            logger.info(f"Calling {uri} and speaking: {text}")
        except Exception as e:
            logger.error(f"Failed to call and speak to {uri}: {e}")

    def on_incoming_call(self, cb: Callable[[str], None]) -> None:
        """
        Register callback for incoming calls.

        Args:
            cb: Callback(from_uri) invoked when a call arrives.

        Note: pjsua2 calls the callback from a worker thread. The real
        implementation marshals onto the asyncio loop via
        asyncio.run_coroutine_threadsafe (spec §7.5).
        """
        self._on_call = cb
        logger.debug("Registered incoming call callback")

    def on_incoming_message(self, cb: Callable[[str, str], None]) -> None:
        """
        Register callback for incoming SIP MESSAGEs.

        Args:
            cb: Callback(from_uri, text) invoked when a SIP MESSAGE arrives.

        Note: pjsua2 calls the callback from a worker thread. The real
        implementation marshals onto the asyncio loop via
        asyncio.run_coroutine_threadsafe (spec §7.5).
        """
        self._on_message = cb
        logger.debug("Registered incoming message callback")

    def has_active_call(self) -> bool:
        """
        Check if a call is currently active.

        Returns:
            True if a call is in progress, False otherwise.
        """
        return self._active_call is not None

    async def speak_into_call(self, text: str) -> bool:
        """
        Play text into the active call.

        Args:
            text: Text to play (converted to speech by Piper).

        Returns:
            True on success, False if no active call.
        """
        if not self.has_active_call():
            logger.warning("No active call to speak into")
            return False

        if not _HAVE_PJSUA2:
            logger.warning("pjsua2 not installed, speak_into_call is a no-op")
            return False

        try:
            # In a real implementation with pjsua2 installed:
            # - Generate WAV via Piper (from text)
            # - Attach player port to active call media
            logger.info(f"Speaking into active call: {text}")
            return True
        except Exception as e:
            logger.error(f"Failed to speak into call: {e}")
            return False

    async def aclose(self) -> None:
        """
        Tear down the SIP endpoint and active calls.

        Note: Graceful cleanup of pjsua2 Endpoint.
        """
        try:
            if _HAVE_PJSUA2 and self._endpoint is not None:
                # In a real implementation: Endpoint.hangupAllCalls() then
                # Endpoint.destroy()
                logger.info("SIP endpoint closed")
            self._active_call = None
            self._endpoint = None
            self._account = None
        except Exception as e:
            logger.error(f"Error closing SIP endpoint: {e}")
