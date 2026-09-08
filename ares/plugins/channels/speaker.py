"""Speaker channel: announce ARES's speech on a Home Assistant media_player.

This is the "you're home but not at the dashboard" delivery path. When the
router can't reach the user on their active channel (e.g. the web session is
abandoned), it tries SPEAKER before PUSH: if a configured person is home, ARES
speaks the message aloud on a room speaker via HA text-to-speech; if no one is
home, or HA is unreachable, deliver() returns False and the router falls
through to PUSH (the user's phone).

The Home Assistant service is injected (duck-typed), never imported, so this
plugin does not depend on the HA source plugin.
"""
from __future__ import annotations

import typing

from ares.core.channel import BaseChannel, ChannelType
from ares.core.utils.logging import get_logger

if typing.TYPE_CHECKING:
    from ares.core.session import Session


logger = get_logger(__name__)


class SpeakerChannel(BaseChannel):
    """Deliver a message as a spoken TTS announcement on an HA media_player."""

    type = ChannelType.SPEAKER

    def __init__(
        self,
        ha_service: typing.Any,
        presence_entities: list[str],
        service_entity: str,
        service_data: dict | None = None,
        tts_domain: str = "tts",
        tts_service: str = "speak",
        tts_field: str = "message",
        language: str | None = None,
    ) -> None:
        """
        Initialize the speaker channel.

        The call is expressed generically so it fits both HA TTS shapes:

        * modern `tts.speak` — ``service_entity`` is the TTS engine entity
          (e.g. ``tts.piper``) and ``service_data`` names the target speaker,
          e.g. ``{"media_player_entity_id": "media_player.living_room_nest"}``;
        * legacy ``tts.<engine>_say`` — ``service_entity`` is the media_player
          itself and ``service_data`` is empty.

        Args:
            ha_service: The Home Assistant service (duck-typed: needs
                get_state(entity_id) and call_service(domain, service,
                entity_id, data)).
            presence_entities: Entities whose state == "home" means someone is
                present (e.g. ["person.raka"]). Empty disables the channel.
            service_entity: The entity_id the TTS service is called against.
                Empty disables the channel.
            service_data: Static data merged into every call (e.g. the target
                media_player for `tts.speak`).
            tts_domain: HA service domain for TTS (default "tts").
            tts_service: HA TTS service to call (default "speak").
            tts_field: The message field name the TTS service expects
                (default "message").
            language: Optional language code passed to the TTS service.
        """
        self.ha = ha_service
        self.presence_entities = list(presence_entities or [])
        self.service_entity = service_entity
        self.service_data = dict(service_data or {})
        self.tts_domain = tts_domain
        self.tts_service = tts_service
        self.tts_field = tts_field
        self.language = language

    async def anyone_home(self) -> bool:
        """True if any configured presence entity currently reads "home"."""
        for eid in self.presence_entities:
            try:
                state = await self.ha.get_state(eid)
            except Exception as e:
                # A presence read that fails is not "away" — but we can't
                # confirm "home" either, so skip this entity and try the rest.
                logger.warning("speaker: presence read failed for %s: %s", eid, e)
                continue
            if str(state.get("state", "")).lower() == "home":
                return True
        return False

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """
        Announce the message on the speaker if someone is home.

        Returns:
            True if the announcement was sent; False if the channel is
            unconfigured (no service_entity or presence entities), no one is
            home, or the TTS call failed (so the router falls through to PUSH).
        """
        if not self.service_entity or not self.presence_entities:
            return False
        try:
            if not await self.anyone_home():
                return False
            data: dict[str, typing.Any] = {**self.service_data, self.tts_field: message}
            if self.language:
                data["language"] = self.language
            await self.ha.call_service(
                self.tts_domain, self.tts_service, self.service_entity, data
            )
            logger.info("speaker: announced via %s for %s", self.service_entity, user_id)
            return True
        except Exception as e:
            logger.error("speaker: announce failed for %s: %s", user_id, e)
            return False
