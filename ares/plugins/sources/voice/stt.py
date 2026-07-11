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
            # Transcribe using the model; segments is a generator
            segments, info = self.model.transcribe(audio, language="en")

            # Join all segment texts
            transcript = "".join(segment.text for segment in segments).strip()

            # Spec §7.4: empty/whitespace transcript → drop (return empty string)
            if not transcript:
                log.debug("Transcription produced empty/whitespace result")
                return ""

            log.debug(f"Transcribed: {transcript}")
            return transcript

        except Exception as e:
            log.error(f"Transcription failed: {e}")
            raise RuntimeError(f"Whisper transcription failed: {e}") from e
