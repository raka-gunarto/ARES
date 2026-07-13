"""SIP source (spec §7.5).

`SIPSource` (name `sip`) drives an injected `SIPService` (pjsua2 wrapper,
provided by `ares.plugins.sip.client`, not imported here). This module never
imports `pjsua2` itself so it can be imported and config-validated without the
`sip` extra installed (spec §10, M8 import-level test).

Incoming SIP MESSAGE -> `type="sip_message"` event. Incoming call from a known
user URI -> answered, greeted, and looped: record until silence -> Whisper ->
`type="call_speech"` event; TTS replies stream back via `SIPCallChannel` while
the call is up. Calls from unknown URIs are rejected and logged.

pjsua2 callbacks arrive on non-asyncio threads; they are marshalled onto the
main loop with `asyncio.run_coroutine_threadsafe`.
"""

from __future__ import annotations

import asyncio

from ares.core.config import ConfigError
from ares.core.event import Priority
from ares.core.source import BaseSource
from ares.core.utils.logging import get_logger

log = get_logger(__name__)


class SIPSource(BaseSource):
    """SIP source driving an injected `SIPService` (spec §7.5).

    One instance per daemon. Registers callbacks with the service for
    incoming messages and calls, and marshals them onto the main event loop.
    """

    name = "sip"

    def __init__(self, bus, config: dict, service) -> None:
        """Initialize the SIP source.

        Args:
            bus: The EventBus to publish events to.
            config: Configuration dictionary for this source. Must contain
                `server` and `username`.
            service: The `SIPService` instance (pjsua2 wrapper) to drive, or
                None in structural tests where no callback wiring occurs.

        Raises:
            ConfigError: If `server` or `username` is missing from config.
        """
        super().__init__(bus, config)

        if not config.get("server"):
            raise ConfigError("sip: server is required")
        if not config.get("username"):
            raise ConfigError("sip: username is required")

        self.service = service
        self._loop: asyncio.AbstractEventLoop | None = None

        if self.service is not None:
            self.service.on_incoming_message(self._on_message)
            self.service.on_incoming_call(self._on_call)

    def _on_message(self, from_uri: str, text: str) -> None:
        """Handle an incoming SIP MESSAGE callback (pjsua2 thread).

        Marshals emission of a `sip_message` event onto the main loop.

        Args:
            from_uri: The SIP URI the message came from.
            text: The message text.
        """
        coro = self.emit(
            type="sip_message",
            payload={"text": text, "from_uri": from_uri},
            priority=Priority.NORMAL,
            user_id="primary",
        )
        self._schedule(coro)

    def _on_call(self, from_uri: str) -> None:
        """Handle an incoming SIP call callback (pjsua2 thread).

        Rejects calls from URIs not in `service.user_uris`; otherwise
        schedules the in-call handling loop on the main loop.

        Args:
            from_uri: The SIP URI the call came from.
        """
        # Match leniently (substring), consistent with SIPService — the incoming
        # From URI carries angle brackets / display names / ports, e.g.
        # '<sip:phone@172.16.0.1>', so exact set membership wrongly rejects it.
        allowed = list(self.service.user_uris.values()) if self.service else []
        if not any(a in from_uri for a in allowed):
            log.warning("sip: rejecting call from unknown uri %s", from_uri)
            return

        self._schedule(self._handle_call(from_uri))

    def _schedule(self, coro) -> None:
        """Schedule a coroutine onto the captured main loop.

        Uses `run_coroutine_threadsafe` when called from a foreign (pjsua2)
        thread. Falls back to `create_task` if the current thread already
        has a running loop matching the captured one (e.g. in tests), and
        logs a warning if no loop has been captured yet.

        Args:
            coro: The coroutine to run.
        """
        if self._loop is None:
            log.warning("sip: no event loop captured yet; dropping callback")
            coro.close()
            return

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is self._loop:
            self._loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _handle_call(self, from_uri: str) -> None:
        """Handle an accepted incoming call end-to-end (spec §7.5).

        Answers, plays the configured greeting, then loops: record until
        700 ms silence (or configured timeout) -> Whisper -> emit
        `type="call_speech"`. TTS replies stream back into the call via the
        `SIPCallChannel` while the call is up (handled by the router/channel,
        not here). Exits when the call is no longer active.

        The actual audio record/transcribe path (PJSIP WAV recorder ports +
        Whisper) is driven through `self.service`; failures are caught so a
        misbehaving call never crashes the daemon.

        Args:
            from_uri: The SIP URI the call came from (already validated as
                an allowed user URI).
        """
        try:
            greeting = self.config.get("greeting", "")
            if greeting:
                # The call is already answered by the service; speak the greeting
                # INTO it (call_and_speak would place a new outbound call).
                await self.service.speak_into_call(greeting)

            while self.service.has_active_call():
                if self._stopping:
                    break

                transcript = await self._record_and_transcribe()
                if not transcript:
                    continue

                await self.emit(
                    type="call_speech",
                    payload={"text": transcript},
                    priority=Priority.NORMAL,
                    user_id="primary",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sip: error handling call from %s", from_uri)

    async def _record_and_transcribe(self) -> str | None:
        """Record one utterance from the active call and transcribe it.

        Delegates to the SIPService, which records via a PJSIP WAV recorder
        port (for a configured window) and transcribes with Whisper. Returns
        None if there is nothing to transcribe or the call has ended.

        Returns:
            The transcript text, or None.
        """
        return await self.service.record_utterance()

    async def start(self) -> None:
        """Register the SIP account and idle while running.

        Captures the running loop so thread-based callbacks
        (`_on_message`/`_on_call`) can marshal work back onto it. Long
        running; returns only when `stop()` is called. If `service` is None
        (should not happen in prod, only in structural tests), logs and
        idles without registering.
        """
        self._loop = asyncio.get_running_loop()

        if self.service is None:
            log.warning("sip: no SIPService injected; source idle")
            while not self._stopping:
                await asyncio.sleep(1)
            return

        try:
            await self.service.register()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sip: registration failed; source idle")

        try:
            while not self._stopping:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
