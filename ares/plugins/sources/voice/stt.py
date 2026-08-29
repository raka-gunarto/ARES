"""Faster-Whisper wrapper for speech-to-text transcription.

Per spec §7.4 step 2: STT uses faster-whisper (WhisperModel) with CPU int8
computation (default). Empty or whitespace transcripts are dropped.

Faster-whisper is lazily loaded via try/except at module level; this module
imports cleanly even if the `voice` extra is not installed. Instantiation of
WhisperSTT raises RuntimeError if the library is missing.
"""

from __future__ import annotations

from ares.core.utils.logging import get_logger

log = get_logger(__name__)

# Guarded import of heavy libraries
try:
    from faster_whisper import WhisperModel

    _HAVE_STT = True
except ImportError:
    _HAVE_STT = False


# --- Hallucination filter (§7.4/§7.5) ---------------------------------------
# Whisper invents stock phrases out of near-silence. In the live SIP trace 36 of
# 78 in-call turns were <=2 words and 30 of them were the single word "You" —
# each one a phantom `call_speech` event costing a full serialized agent cycle
# mid-call, so ARES answered a word the caller never said. `vad_filter=True`
# reduces this but does not eliminate it, because a pass containing a cough or
# line noise still reaches the decoder.
#
# Two independent gates, both cheap:
#  1. Whisper's own `no_speech_prob` per segment — its estimate that the segment
#     is not speech at all. Segments over the threshold are dropped outright.
#  2. An exact-match phrase list, applied only when the phrase is the ENTIRE
#     transcript. A bare "you" or "thanks for watching" is never a real turn on
#     a phone call; losing a genuine one-word courtesy costs nothing, while
#     answering a phantom one derails the conversation.
_NO_SPEECH_PROB_MAX = 0.6

_HALLUCINATION_PHRASES = frozenset(
    {
        "you",
        "thank you",
        "thank you very much",
        "thanks for watching",
        "thank you for watching",
        "please subscribe",
        "subscribe",
        "bye bye",
        "the end",
        "blank_audio",
        "silence",
        "music",
        "applause",
    }
)


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation/brackets for stop-list comparison."""
    stripped = "".join(c for c in text.lower() if c.isalnum() or c.isspace())
    return " ".join(stripped.split())


def is_hallucination(transcript: str) -> bool:
    """True if `transcript` is entirely one of Whisper's silence artefacts."""
    return _normalize(transcript) in _HALLUCINATION_PHRASES


class WhisperSTT:
    """Faster-Whisper wrapper for speech-to-text transcription.

    Transcribes audio to text using the Faster-Whisper model with CPU int8
    computation (per spec §7.4). Empty or whitespace-only transcripts are
    treated as empty strings and dropped by the voice source.

    Requires the `voice` extra to be installed:
        pip install -e ".[voice]"

    Raises:
        RuntimeError: If faster-whisper is not installed.
    """

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        """Initialize Whisper STT model.

        Args:
            model_size: Model size ("tiny", "base", "small", "medium", "large").
                Default "small" per spec §7.4.
            device: Device to run on ("cpu" or "cuda"). Default "cpu".
            compute_type: Computation type ("int8", "int8_float32", "int8_float16",
                "float32", "float16", "bfloat16"). Default "int8" for CPU per spec §7.4.

        Raises:
            RuntimeError: If faster-whisper is not installed.
        """
        if not _HAVE_STT:
            raise RuntimeError(
                "faster-whisper is not installed. "
                "Install it with: pip install -e '.[voice]'"
            )

        log.debug(
            f"Initializing Whisper STT (model={model_size}, device={device}, "
            f"compute_type={compute_type})"
        )
        self.model = WhisperModel(
            model_size, device=device, compute_type=compute_type
        )
        log.debug(f"Whisper STT initialized")

    def transcribe(self, audio) -> str:
        """Transcribe audio to text.

        Args:
            audio: Audio data (PCM samples, numpy array, or bytes).

        Returns:
            Transcribed text. Empty string if transcript is empty or whitespace-only
            (per spec §7.4: empty/whitespace → drop).

        Raises:
            RuntimeError: If transcription fails.
        """
        try:
            # Transcribe using the model; segments is a generator.
            # vad_filter drops non-speech regions before decoding — without it
            # Whisper reliably hallucinates stock phrases ("you", "Thank you.")
            # out of near-silence. condition_on_previous_text=False stops one
            # such hallucination from seeding the next segment.
            segments, info = self.model.transcribe(
                audio,
                language="en",
                vad_filter=True,
                condition_on_previous_text=False,
            )

            # Drop segments Whisper itself scores as non-speech before joining.
            # getattr keeps this working if a faster-whisper build omits the
            # field rather than crashing the call.
            kept = []
            for segment in segments:
                prob = getattr(segment, "no_speech_prob", 0.0) or 0.0
                if prob > _NO_SPEECH_PROB_MAX:
                    log.debug(
                        "dropping non-speech segment (no_speech_prob=%.2f): %r",
                        prob,
                        segment.text,
                    )
                    continue
                kept.append(segment.text)
            transcript = "".join(kept).strip()

            # Spec §7.4: empty/whitespace transcript → drop (return empty string)
            if not transcript:
                log.debug("Transcription produced empty/whitespace result")
                return ""

            # Stock phrases invented from silence are not turns — drop them so
            # they never become an event (30 phantom "You" turns in the trace).
            if is_hallucination(transcript):
                log.debug("dropping known Whisper hallucination: %r", transcript)
                return ""

            log.debug(f"Transcribed: {transcript}")
            return transcript

        except Exception as e:
            log.error(f"Transcription failed: {e}")
            raise RuntimeError(f"Whisper transcription failed: {e}") from e
