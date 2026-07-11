"""Per-room voice source (spec §7.4).

One `VoiceSource` instance per configured room; name `voice`. Pipeline:
capture audio via `sounddevice` -> Silero VAD segmentation -> faster-whisper
transcription -> intent filter -> emit `type="speech"` event.

`sounddevice`/`numpy` are the `voice` extra and may not be installed; this
module must import cleanly without them. When absent, `start()` logs a
warning and idles instead of opening an audio stream.
"""

from __future__ import annotations

import asyncio

from ares.core.config import ConfigError
from ares.core.event import Priority
from ares.core.source import BaseSource
from ares.core.utils.logging import get_logger

# Sibling voice wrappers (same plugin package) -- imported for type hints only.
# The heavy models are instantiated by the caller and passed in, not here.
from ares.plugins.sources.voice.intent import IntentFilter
from ares.plugins.sources.voice.stt import WhisperSTT
from ares.plugins.sources.voice.vad import SileroVAD

log = get_logger(__name__)

# Guarded import of heavy audio libraries (voice extra)
try:
    import numpy as np
    import sounddevice as sd

    _HAVE_AUDIO = True
except ImportError:
    _HAVE_AUDIO = False

# spec §7.4 step 1: 30 ms frames, 700 ms silence ends a segment, 30 s hard cap.
FRAME_MS = 30
SILENCE_MS = 700
MAX_SEGMENT_S = 30


class VoiceSource(BaseSource):
    """Per-room voice pipeline source (spec §7.4).

    One instance per configured room. Captures audio from `input_device`,
    runs VAD segmentation, transcribes segments with Whisper, filters by
    intent, and emits `type="speech"` events for segments that pass.
    """

    name = "voice"

    def __init__(
        self,
        bus,
        config: dict,
        room: str,
        input_device,
        vad: SileroVAD | None,
        stt: WhisperSTT | None,
        intent: IntentFilter | None,
        mute_event: asyncio.Event | None = None,
    ) -> None:
        """Initialize the voice source for one room.

        Args:
            bus: The EventBus to publish events to.
            config: Configuration dictionary for this source.
            room: Room identifier this instance serves. Required.
            input_device: `sounddevice` input device identifier. Required.
            vad: SileroVAD instance used for speech segmentation.
            stt: WhisperSTT instance used for transcription.
            intent: IntentFilter instance used to gate segments.
            mute_event: Shared `asyncio.Event` set while this room's TTS
                channel is playing audio; while set, incoming frames are
                ignored so ARES does not transcribe its own speech.

        Raises:
            ConfigError: If `room` is empty/None or `input_device` is None.
        """
        super().__init__(bus, config)

        if not room:
            raise ConfigError("voice: room is required")
        if input_device is None:
            raise ConfigError(f"voice[{room}]: input_device is required")

        self.room = room
        self.input_device = input_device
        self.vad = vad
        self.stt = stt
        self.intent = intent
        self.mute_event = mute_event
        self.sample_rate = 16000

    async def start(self) -> None:
        """Run the capture -> VAD -> STT -> intent -> emit pipeline.

        Long-running; returns only when `stop()` is called (i.e. when
        `self._stopping` becomes True). If the `voice` extra is not
        installed, logs a warning and idles instead of opening a stream.
        """
        if not _HAVE_AUDIO:
            log.warning(
                "voice[%s]: sounddevice/numpy not installed (voice extra); "
                "source idle",
                self.room,
            )
            while not self._stopping:
                await asyncio.sleep(1)
            return

        loop = asyncio.get_running_loop()
        frame_samples = int(self.sample_rate * FRAME_MS / 1000)
        frame_queue: asyncio.Queue = asyncio.Queue()

        def _callback(indata, frames, time_info, status) -> None:
            """sounddevice callback; runs on a separate thread."""
            if status:
                log.warning("voice[%s]: input stream status: %s", self.room, status)
            frame_bytes = bytes(indata)
            loop.call_soon_threadsafe(frame_queue.put_nowait, frame_bytes)

        try:
            stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=frame_samples,
                device=self.input_device,
                channels=1,
                dtype="int16",
                callback=_callback,
            )
        except Exception as exc:
            log.error(
                "voice[%s]: failed to open input stream on device %r: %s",
                self.room,
                self.input_device,
                exc,
            )
            while not self._stopping:
                await asyncio.sleep(1)
            return

        segment_frames: list[bytes] = []
        in_speech = False
        silence_ms = 0
        segment_ms = 0

        try:
            with stream:
                while not self._stopping:
                    try:
                        frame = await asyncio.wait_for(
                            frame_queue.get(), timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        continue

                    try:
                        # While TTS is playing in this room, ignore frames so
                        # ARES does not transcribe its own speech.
                        if self.mute_event is not None and self.mute_event.is_set():
                            continue

                        is_speech = bool(self.vad and self.vad.is_speech(frame))

                        if is_speech:
                            in_speech = True
                            silence_ms = 0
                            segment_frames.append(frame)
                            segment_ms += FRAME_MS
                        elif in_speech:
                            segment_frames.append(frame)
                            segment_ms += FRAME_MS
                            silence_ms += FRAME_MS

                        segment_ended = in_speech and (
                            silence_ms >= SILENCE_MS
                            or segment_ms >= MAX_SEGMENT_S * 1000
                        )

                        if segment_ended:
                            audio = b"".join(segment_frames)
                            segment_frames = []
                            in_speech = False
                            silence_ms = 0
                            segment_ms = 0
                            await self._process_segment(audio)

                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(
                            "voice[%s]: error processing audio frame/segment",
                            self.room,
                        )
                        segment_frames = []
                        in_speech = False
                        silence_ms = 0
                        segment_ms = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("voice[%s]: input stream failed", self.room)

    async def _process_segment(self, segment_audio: bytes) -> None:
        """Transcribe, filter, and emit a completed speech segment.

        Args:
            segment_audio: Raw PCM audio bytes for the completed segment.
        """
        if self.stt is None:
            return

        transcript = self.stt.transcribe(segment_audio)
        if not transcript or not transcript.strip():
            return

        if self.intent is None:
            passed = True
        else:
            passed = await self.intent.passes(transcript, audio=segment_audio)

        if passed:
            await self.emit(
                type="speech",
                payload={"text": transcript},
                priority=Priority.NORMAL,
                room=self.room,
                user_id="primary",
            )
