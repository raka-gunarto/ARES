"""Silero VAD wrapper for voice activity detection.

Per spec §7.4 step 1: VAD runs on 30 ms audio frames; a speech segment starts
when speech is detected and ends after 700 ms of silence (hard cap 30 s total).

Silero VAD is lazily loaded via try/except at module level; this module imports
cleanly even if the `voice` extra (with silero-vad and numpy) is not installed.
Instantiation of SileroVAD raises RuntimeError if the libs are missing.
"""

from __future__ import annotations

from ares.core.utils.logging import get_logger

log = get_logger(__name__)

# Guarded imports of heavy libraries
try:
    import numpy as np
    import torch
    from silero_vad import load_silero_vad, get_speech_timestamps

    _HAVE_VAD = True
except ImportError:
    _HAVE_VAD = False

# Module constants (spec §7.4)
FRAME_MS = 30  # 30 ms audio frames
SILENCE_MS = 700  # Speech segment ends after 700 ms of silence
MAX_SEGMENT_S = 30  # Hard cap on segment length


class SileroVAD:
    """Silero VAD wrapper for detecting speech in audio frames.

    Detects voice activity on 30 ms frames using the Silero VAD model.
    Speech segments end after 700 ms of silence (spec §7.4).

    Requires the `voice` extra to be installed:
        pip install -e ".[voice]"

    Raises:
        RuntimeError: If silero-vad or numpy is not installed.
    """

    def __init__(self, sample_rate: int = 16000) -> None:
        """Initialize Silero VAD.

        Args:
            sample_rate: Audio sample rate in Hz (default 16000).

        Raises:
            RuntimeError: If silero-vad or numpy is not installed.
        """
        if not _HAVE_VAD:
            raise RuntimeError(
                "silero-vad and numpy are not installed. "
                "Install them with: pip install -e '.[voice]'"
            )

        self.sample_rate = sample_rate
        self.model = load_silero_vad()
        log.debug(f"Silero VAD initialized (sample_rate={sample_rate} Hz)")

    def is_speech(self, frame: bytes | bytearray | list) -> bool:
        """Check if a 30 ms audio frame contains speech.

        Args:
            frame: Audio frame data (typically 480 samples at 16 kHz for 30 ms).

        Returns:
            True if the frame likely contains speech; False otherwise.
                Threshold is approximately 0.5 on the VAD probability.
        """
        if not isinstance(frame, (bytes, bytearray, list)):
            log.warning(f"is_speech: unexpected frame type {type(frame)}")
            return False

        try:
            # Convert frame to numpy array as float32 (normalized)
            if isinstance(frame, (bytes, bytearray)):
                audio_frame = np.frombuffer(frame, dtype=np.int16).astype(
                    np.float32
                ) / 32768.0
            else:
                audio_frame = np.array(frame, dtype=np.float32)

            # Convert to torch tensor and get VAD probability (0.0-1.0); threshold ~0.5
            audio_tensor = torch.from_numpy(audio_frame)
            prob = self.model(audio_tensor, self.sample_rate)
            # Probability is typically a tensor; extract scalar value
            if isinstance(prob, torch.Tensor):
                prob = prob.item()
            return prob > 0.5

        except Exception as e:
            log.warning(f"is_speech: VAD evaluation failed: {e}")
            return False
