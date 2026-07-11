"""Voice TTS delivery channel via Piper + sounddevice (spec §7.4)."""
from __future__ import annotations

import asyncio
import tempfile
import typing
import wave
from pathlib import Path

from ares.core.channel import BaseChannel, ChannelType
from ares.core.utils.logging import get_logger

if typing.TYPE_CHECKING:
    from ares.core.session import Session

try:
    import numpy as np
    import sounddevice as sd

    _HAVE_AUDIO = True
except ImportError:
    _HAVE_AUDIO = False

logger = get_logger(__name__)


class VoiceTTSChannel(BaseChannel):
    """Message delivery channel that synthesises speech via Piper and plays
    it back on a room's output device (spec §7.4).

    Room resolution at delivery time, in order:
        1. ``session.current_room`` if set and known.
        2. The last room a message was delivered to, if known.
        3. The configured default room.
    """

    type = ChannelType.VOICE

    def __init__(
        self,
        rooms: dict,
        default_room: str,
        piper_model: str,
        mute_events: dict | None = None,
    ) -> None:
        """
        Initialize the voice TTS channel.

        Args:
            rooms: Mapping of room name -> {"input_device", "output_device"}.
            default_room: Room to fall back to when none can be resolved.
            piper_model: Piper model name/path passed to ``piper --model``.
            mute_events: Mapping of room name -> asyncio.Event, set while TTS
                is playing in that room so the room's VAD mutes itself.
        """
        self.rooms = rooms
        self.default_room = default_room
        self.piper_model = piper_model
        self.mute_events: dict = mute_events or {}
        self._last_room: str | None = None

    def _resolve_room(self, session: Session) -> str | None:
        """
        Resolve which room to play audio in.

        Args:
            session: The user's current session.

        Returns:
            A room name known to ``self.rooms``, or None if unresolvable.
        """
        if session.current_room is not None and session.current_room in self.rooms:
            return session.current_room
        if self._last_room is not None and self._last_room in self.rooms:
            return self._last_room
        if self.default_room in self.rooms:
            return self.default_room
        return None

    async def _synthesize(self, message: str, wav_path: Path) -> bool:
        """
        Synthesize ``message`` to a WAV file at ``wav_path`` using Piper.

        Args:
            message: The text to synthesize.
            wav_path: Destination path for the WAV output.

        Returns:
            True on success, False if piper is missing or fails.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "piper",
                "--model",
                self.piper_model,
                "--output_file",
                str(wav_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("piper binary not found — cannot synthesize speech")
            return False

        assert proc.stdin is not None
        proc.stdin.write(message.encode())
        proc.stdin.close()
        try:
            await proc.stdin.wait_closed()
        except Exception:  # noqa: BLE001 - best-effort stdin close
            pass

        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "piper exited with code %s: %s", proc.returncode, stderr.decode(errors="replace")
            )
            return False
        return True

    def _play_sync(self, wav_path: Path, output_device: str) -> None:
        """
        Blocking playback of a WAV file on the given output device.

        Must only be called when ``_HAVE_AUDIO`` is True; run via
        ``asyncio.to_thread`` from async code.

        Args:
            wav_path: Path to the WAV file to play.
            output_device: The sounddevice output device name/index.
        """
        with wave.open(str(wav_path), "rb") as wf:
            n_frames = wf.getnframes()
            samplerate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            n_channels = wf.getnchannels()
            raw = wf.readframes(n_frames)

        dtype_map = {1: np.int8, 2: np.int16, 4: np.int32}
        dtype = dtype_map.get(sampwidth, np.int16)
        data = np.frombuffer(raw, dtype=dtype)
        if n_channels > 1:
            data = data.reshape(-1, n_channels)

        sd.play(data, samplerate, device=output_device)
        sd.wait()

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """
        Synthesize ``message`` with Piper and play it in the resolved room.

        Args:
            user_id: The user ID (unused; voice delivery is room-scoped).
            message: The text to speak.
            session: The user's current session (used for room resolution).

        Returns:
            True on successful playback, False on any failure.
        """
        wav_path: Path | None = None
        room: str | None = None
        ev = None
        try:
            room = self._resolve_room(session)
            if room is None:
                logger.error(
                    "voice_tts: could not resolve a room (current=%s, last=%s, default=%s)",
                    session.current_room,
                    self._last_room,
                    self.default_room,
                )
                return False

            room_config = self.rooms.get(room)
            if room_config is None:
                logger.error("voice_tts: resolved room %r not in configured rooms", room)
                return False

            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            wav_path = Path(tmp.name)
            tmp.close()

            ok = await self._synthesize(message, wav_path)
            if not ok:
                return False

            if not _HAVE_AUDIO:
                logger.warning(
                    "audio playback unavailable — voice extra not installed"
                )
                return False

            ev = self.mute_events.get(room)
            if ev is not None:
                ev.set()
            try:
                output_device = room_config.get("output_device")
                await asyncio.to_thread(self._play_sync, wav_path, output_device)
            finally:
                if ev is not None:
                    ev.clear()

            return True
        except Exception:  # noqa: BLE001 - deliver must never raise
            logger.exception("voice_tts: delivery failed")
            return False
        finally:
            if room is not None:
                self._last_room = room
            if wav_path is not None:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    pass
