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


# ---- _wait_for_utterance endpointing (spec §7.5) ---------------------------
#
# Driven against a real growing WAV written by a background thread, the same
# way the PJSIP recorder feeds it. SIPService is built with __new__ so these
# run without pjsua2 installed.

import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from ares.plugins.sip import client as sip_client  # noqa: E402
from ares.plugins.sip.client import SIPService  # noqa: E402

_LOUD = _pcm16([6000, -6000] * 800)   # ~100 ms of speech-level audio at 16 kHz
_QUIET = _pcm16([5, -5] * 800)        # ~100 ms of silence


class _FakeCall:
    def __init__(self) -> None:
        self.disconnected = threading.Event()


def _service(**over) -> SIPService:
    svc = SIPService.__new__(SIPService)
    svc.record_seconds = over.get("record_seconds", 5)
    svc.silence_seconds = over.get("silence_seconds", 0.3)
    svc.silence_rms_threshold = over.get("silence_rms_threshold", 500)
    svc._abort_record = threading.Event()
    svc._speaking = threading.Event()
    return svc


class _Writer:
    """Appends PCM to a WAV on a background thread, mimicking the recorder."""

    def __init__(self, path: Path) -> None:
        _write_wav(path, b"")  # header only
        self.path = path
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def run(self, script: list[bytes], then: bytes = _QUIET) -> None:
        def _pump() -> None:
            for chunk in script:
                if self.stop.is_set():
                    return
                with open(self.path, "ab") as fh:
                    fh.write(chunk)
                time.sleep(0.1)
            while not self.stop.is_set():  # trail off with `then` forever
                with open(self.path, "ab") as fh:
                    fh.write(then)
                time.sleep(0.1)

        self.thread = threading.Thread(target=_pump, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)


@pytest.fixture
def writer(tmp_path: Path):
    w = _Writer(tmp_path / "utt.wav")
    yield w
    w.close()


def test_speech_then_silence_ends_the_turn(writer: _Writer) -> None:
    svc = _service(silence_seconds=0.3)
    writer.run([_LOUD] * 4)  # ~400 ms of speech, then silence forever
    assert svc._wait_for_utterance(_FakeCall(), str(writer.path)) is True


def test_pure_silence_reports_no_speech(writer: _Writer, monkeypatch) -> None:
    """A pass that hears nothing must NOT be transcribed (phantom-turn guard)."""
    monkeypatch.setattr(sip_client, "_MAX_IDLE_SECONDS", 0.5)
    svc = _service()
    writer.run([])  # silence only
    assert svc._wait_for_utterance(_FakeCall(), str(writer.path)) is False


def test_abort_cuts_the_pass_short(writer: _Writer) -> None:
    """speak_into_call/hangup set the abort flag to free the pjsua2 thread."""
    svc = _service()
    writer.run([_LOUD] * 2)
    threading.Timer(0.3, svc._abort_record.set).start()

    started = time.monotonic()
    assert svc._wait_for_utterance(_FakeCall(), str(writer.path)) is False
    # Returned on the abort, not after record_seconds (5 s).
    assert time.monotonic() - started < 2.0


def test_disconnect_ends_the_pass(writer: _Writer) -> None:
    svc = _service()
    call = _FakeCall()
    writer.run([_LOUD] * 2)
    threading.Timer(0.3, call.disconnected.set).start()
    assert svc._wait_for_utterance(call, str(writer.path)) is False


def test_hard_cap_starts_at_first_speech(writer: _Writer) -> None:
    """Idle listening must not eat the cap, or a late starter gets clipped."""
    svc = _service(record_seconds=1, silence_seconds=0.3)
    # 600 ms of silence first — longer than the cap would allow if it ran from
    # the moment the pass opened — then speech, then quiet.
    writer.run([_QUIET] * 6 + [_LOUD] * 3)
    assert svc._wait_for_utterance(_FakeCall(), str(writer.path)) is True
