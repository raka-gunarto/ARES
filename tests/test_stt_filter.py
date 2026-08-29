"""Whisper hallucination filtering (spec §7.4/§7.5).

Grounded in the live SIP trace: 36 of 78 in-call turns were <=2 words and 30 of
those were the bare word "You" — Whisper inventing speech out of silence. Each
became a `call_speech` event and consumed a full serialized agent cycle mid-call.
"""
from __future__ import annotations

import dataclasses

from ares.plugins.sources.voice.stt import WhisperSTT, is_hallucination


@dataclasses.dataclass
class _Seg:
    """Stand-in for a faster-whisper segment."""

    text: str
    no_speech_prob: float = 0.0


class _FakeModel:
    def __init__(self, segments):
        self._segments = segments

    def transcribe(self, audio, **kwargs):
        return iter(self._segments), {}


def _stt(segments) -> WhisperSTT:
    """Build a WhisperSTT without loading a real model."""
    obj = object.__new__(WhisperSTT)
    obj.model = _FakeModel(segments)
    return obj


# ---- the phrase list -------------------------------------------------------

def test_the_thirty_phantom_yous_are_dropped():
    assert is_hallucination("You")
    assert is_hallucination("you")
    assert is_hallucination("  YOU.  ")


def test_real_speech_survives():
    for real in (
        "yes",
        "turn the lights on",
        "What's the temperature?",
        "Hang up for me",
        "All right. Bye. Bye",
        "no",
    ):
        assert not is_hallucination(real), real


def test_a_plain_goodbye_is_not_treated_as_noise():
    """'Bye' ends real calls in the trace — it must never be filtered."""
    assert not is_hallucination("Bye")
    assert not is_hallucination("Bye.")


def test_phrase_must_be_the_whole_transcript():
    """A stock phrase inside a real sentence is real speech."""
    assert not is_hallucination("thank you for turning the heating on")


# ---- end-to-end through transcribe() ---------------------------------------

def test_transcribe_drops_a_pure_hallucination():
    assert _stt([_Seg("You")]).transcribe(b"") == ""


def test_transcribe_drops_segments_whisper_scores_as_non_speech():
    out = _stt([_Seg(" You", no_speech_prob=0.95), _Seg(" turn on the fan")]).transcribe(b"")
    assert out == "turn on the fan"


def test_transcribe_keeps_confident_speech():
    assert _stt([_Seg("hello there", no_speech_prob=0.1)]).transcribe(b"") == "hello there"


def test_transcribe_survives_segments_without_the_probability_field():
    class _Bare:
        text = "hello"

    assert _stt([_Bare()]).transcribe(b"") == "hello"


def test_all_segments_non_speech_yields_empty():
    assert _stt([_Seg("You", 0.99), _Seg(" Thank you.", 0.98)]).transcribe(b"") == ""
