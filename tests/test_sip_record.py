"""Tests for in-call silence endpointing helpers (spec §7.5).

Import-level only: the RMS meter and WAV data-offset parser are pure stdlib
helpers on `ares.plugins.sip.client`, so they exercise without pjsua2/voice
extras. The `_wait_for_utterance` loop itself drives a live pjsua2 recorder and
is not unit-tested here.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

from ares.plugins.sip.client import _rms_int16, _wav_data_offset


def _pcm16(samples: list[int]) -> bytes:
    return struct.pack("<%dh" % len(samples), *samples)


def test_rms_of_silence_is_zero() -> None:
    assert _rms_int16(_pcm16([0] * 100)) == 0.0


def test_rms_empty_and_odd_bytes() -> None:
    assert _rms_int16(b"") == 0.0
    assert _rms_int16(b"\x01") == 0.0  # single stray byte, no whole sample


def test_rms_constant_amplitude() -> None:
    # A constant |amplitude| signal has RMS equal to that amplitude.
    level = _rms_int16(_pcm16([2000, -2000] * 50))
    assert math.isclose(level, 2000.0, rel_tol=1e-6)


def test_rms_speech_far_above_silence_threshold() -> None:
    quiet = _rms_int16(_pcm16([50, -40, 30, -20] * 40))
    loud = _rms_int16(_pcm16([6000, -6500, 7000, -5500] * 40))
    assert quiet < 500 < loud


def _write_wav(path: Path, frames: bytes, rate: int = 16000, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)


def test_data_offset_points_at_pcm_payload(tmp_path: Path) -> None:
    wav = tmp_path / "u.wav"
    payload = _pcm16([123, -456, 789, -321])
    _write_wav(wav, payload)

    offset = _wav_data_offset(str(wav))
    assert offset is not None

    tail = wav.read_bytes()[offset:]
    # The bytes at the reported offset are exactly the PCM we wrote.
    assert tail == payload


def test_data_offset_stable_while_growing(tmp_path: Path) -> None:
    # Simulate the recorder mid-write: a valid header but a placeholder data
    # size of 0, followed by real samples appended after the header.
    wav = tmp_path / "u.wav"
    _write_wav(wav, b"")  # header with an empty data chunk
    header = wav.read_bytes()
    offset = _wav_data_offset(str(wav))
    assert offset is not None

    payload = _pcm16([1, 2, 3, 4, 5, 6])
    wav.write_bytes(header + payload)
    # Offset is unchanged and the appended PCM reads back cleanly.
    assert _wav_data_offset(str(wav)) == offset
    assert wav.read_bytes()[offset:] == payload


def test_data_offset_none_on_garbage(tmp_path: Path) -> None:
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not a wav file at all")
    assert _wav_data_offset(str(junk)) is None


def test_data_offset_none_on_truncated_header(tmp_path: Path) -> None:
    short = tmp_path / "short.bin"
    short.write_bytes(b"RIFF")  # too short to be parseable
    assert _wav_data_offset(str(short)) is None
