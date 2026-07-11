"""Intent filter for voice segments (spec §7.4 step 3).

Three strategies:
  - wake_word: segment passes if wake word fired within it or ≤2 s before
  - llm: LLMClient classification with a fixed prompt
  - hybrid: wake_word OR llm

openWakeWord is lazily loaded via try/except at module level; this module
imports cleanly even if the `voice` extra is not installed.
"""

from __future__ import annotations

from ares.core.llm.client import LLMClient
from ares.core.utils.logging import get_logger

log = get_logger(__name__)

# Guarded import of heavy libraries
try:
    import openwakeword

    _HAVE_WAKE = True
except ImportError:
    _HAVE_WAKE = False


class IntentFilter:
    """Filter transcribed speech segments by intent classification.

    Implements spec §7.4 step 3: determines whether a speech segment should
    be processed based on wake word detection, LLM classification, or hybrid.

    Args:
        strategy: Classification strategy ("wake_word", "llm", or "hybrid").
        wake_word: Wake word phrase (e.g., "hey_ares"). Used in wake_word/hybrid modes.
        llm: LLMClient for classification in llm/hybrid modes (optional, required for llm mode).

    Raises:
        RuntimeError: If wake_word/hybrid strategy is used but openwakeword is not installed.
        ValueError: If strategy is not one of the three known values.
    """

    def __init__(
        self,
        strategy: str,
        wake_word: str,
        llm: LLMClient | None = None,
    ) -> None:
        """Initialize intent filter.

        Args:
            strategy: One of "wake_word", "llm", or "hybrid".
            wake_word: Wake word name/phrase for wake_word and hybrid strategies.
            llm: LLMClient instance (required for llm and hybrid strategies).

        Raises:
            ValueError: If strategy is unknown.
            RuntimeError: If wake_word/hybrid strategy needs openwakeword but it's missing.
        """
        if strategy not in ("wake_word", "llm", "hybrid"):
            raise ValueError(
                f"Unknown intent strategy: {strategy}. "
                "Must be 'wake_word', 'llm', or 'hybrid'."
            )

        self.strategy = strategy
        self.wake_word = wake_word
        self.llm = llm
        self._wake_model = None  # Lazy-loaded

        log.debug(
            f"Intent filter initialized (strategy={strategy}, "
            f"wake_word={wake_word}, llm={'present' if llm else 'absent'})"
        )

    async def passes(self, transcript: str, audio=None) -> bool:
        """Check if a transcript should pass the intent filter.

        Implements spec §7.4 step 3 logic:
          - wake_word: passes if wake word fired within/≤2 s before segment
          - llm: one LLMClient.chat call with fixed classification prompt;
                 passes on YES (case-insensitive)
          - hybrid: wake_word pass OR llm pass

        Args:
            transcript: Transcribed text from the speech segment.
            audio: Raw audio data (bytes or array). Required for wake_word strategy.
                   If None and wake_word check is needed, returns False.

        Returns:
            True if the segment should be processed; False otherwise.

        Raises:
            RuntimeError: If wake_word/hybrid strategy is used but openwakeword is missing.
            RuntimeError: If llm/hybrid strategy is used but llm is None.
        """
        if self.strategy == "wake_word":
            return await self._check_wake_word(audio)
        elif self.strategy == "llm":
            return await self._check_llm(transcript)
        else:  # hybrid
            # Hybrid: pass if either wake_word OR llm check passes
            wake_pass = await self._check_wake_word(audio)
            if wake_pass:
                return True
            return await self._check_llm(transcript)

    async def _check_wake_word(self, audio: bytes | bytearray | list | None) -> bool:
        """Check if wake word was detected in audio (spec §7.4 step 3).

        Wake word detection runs on raw audio; segment passes if the wake word
        fired within it or ≤ 2 seconds before the segment started.

        Args:
            audio: Raw audio data. If None, returns False.

        Returns:
            True if wake word detected within/before segment; False otherwise.

        Raises:
            RuntimeError: If openwakeword is not installed.
        """
        if audio is None:
            log.debug("_check_wake_word: no audio provided, returning False")
            return False

        if not _HAVE_WAKE:
            raise RuntimeError(
                "openwakeword is not installed. "
                "Install it with: pip install -e '.[voice]'"
            )

        try:
            # Lazy-load the wake model on first use
            if self._wake_model is None:
                log.debug(f"Loading openWakeWord model for '{self.wake_word}'")
                self._wake_model = openwakeword.Model(wakeword_names=[self.wake_word])

            # Run detection on the audio
            # Note: audio format/shape depends on the actual audio data.
            # openwakeword.Model expects a numpy array of float32 samples or similar.
            predictions = self._wake_model.predict(audio)

            # Check if wake word fired (predictions is a dict: {word: probability, ...})
            wake_detected = predictions.get(self.wake_word, 0.0) > 0.5

            if wake_detected:
                log.debug(f"Wake word '{self.wake_word}' detected")
                return True

            log.debug(f"Wake word '{self.wake_word}' not detected")
            return False

        except Exception as e:
            log.warning(f"Wake word detection failed: {e}")
            # On error, default to False (don't pass the segment)
            return False

    async def _check_llm(self, transcript: str) -> bool:
        """Check if transcript is addressed to ARES using LLM (spec §7.4 step 3).

        Makes one LLMClient.chat call with a fixed classification prompt.
        Passes if the LLM responds with YES (case-insensitive).

        Args:
            transcript: Transcribed text to classify.

        Returns:
            True if LLM classifies as YES; False otherwise.

        Raises:
            RuntimeError: If llm is None.
        """
        if self.llm is None:
            raise RuntimeError(
                "IntentFilter('llm') or IntentFilter('hybrid') requires an LLMClient, "
                "but none was provided."
            )

        try:
            # Fixed classification prompt per spec §7.4
            system_msg = (
                "Reply YES or NO: is this utterance addressed to a home assistant "
                "named ARES? Utterance: " + transcript
            )

            log.debug(f"Calling LLM for intent classification: {transcript[:50]}...")

            # One chat call with the classification prompt
            response = await self.llm.chat(
                messages=[{"role": "user", "content": system_msg}],
                temperature=0.0,  # Low temperature for deterministic YES/NO
            )

            # Parse the response (look for YES in the content, case-insensitive)
            content = response.get("content", "").upper()
            passed = "YES" in content

            log.debug(f"LLM intent check: {passed} (content={response.get('content')})")
            return passed

        except Exception as e:
            log.warning(f"LLM intent check failed: {e}")
            # On error, default to False (don't pass the segment)
            return False
