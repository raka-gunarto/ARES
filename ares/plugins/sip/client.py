"""SIP service — real pjsua2 wrapper (spec §7.5).

Wraps pjsua2 for account registration, pager-mode SIP MESSAGE, and calls with
speech. Per §7.5 all audio bridging uses PJSIP WAV player/recorder ports against
temp files (Piper for TTS, Whisper for STT) — never live sample streaming.

Threading: pjsua2 is callback/thread based. All pjsua2 *commands* are issued on a
single dedicated worker thread (a max-1 executor, registered with the endpoint);
pjsua2 fires *callbacks* on its own worker thread. Callbacks that need to reach
the asyncio world hand a plain `(from_uri[, text])` tuple to the source-provided
callbacks, which marshal onto the loop with `run_coroutine_threadsafe`.

The `pjsua2` and `faster_whisper` imports are guarded so this module imports
without the `sip`/`voice` extras (spec §10 import-level test); the real code only
runs when the extras are present.
"""
from __future__ import annotations

import array
import asyncio
import concurrent.futures
import contextlib
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable

from ares.core.utils.logging import get_logger

try:
    import pjsua2 as pj

    _HAVE_PJSUA2 = True
except ImportError:
    _HAVE_PJSUA2 = False

logger = get_logger(__name__)


# pjsua2 subclasses can only be defined when pjsua2 is importable.
if _HAVE_PJSUA2:

    class _Call(pj.Call):
        """One pjsua2 call. Signals media-ready and disconnect via Events."""

        def __init__(self, acc, service, call_id=pj.PJSUA_INVALID_ID):
            pj.Call.__init__(self, acc, call_id)
            self._service = service
            self.media_ready = threading.Event()
            self.disconnected = threading.Event()

        def onCallState(self, prm):  # noqa: N802 (pjsua2 API)
            try:
                info = self.getInfo()
                if info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                    self.disconnected.set()
                    self.media_ready.set()  # unblock any waiter
                    self._service._on_call_ended(self)
            except Exception:
                logger.exception("sip: onCallState error")

        def onCallMediaState(self, prm):  # noqa: N802 (pjsua2 API)
            try:
                info = self.getInfo()
                for i, mi in enumerate(info.media):
                    if (
                        mi.type == pj.PJMEDIA_TYPE_AUDIO
                        and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE
                    ):
                        self.media_ready.set()
                        return
            except Exception:
                logger.exception("sip: onCallMediaState error")

        def audio_media(self):
            """Return the call's active AudioMedia, or None."""
            info = self.getInfo()
            for i, mi in enumerate(info.media):
                if (
                    mi.type == pj.PJMEDIA_TYPE_AUDIO
                    and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE
                ):
                    return self.getAudioMedia(i)
            return None

    class _Account(pj.Account):
        """Registers callbacks for incoming calls and pager MESSAGEs."""

        def __init__(self, service):
            pj.Account.__init__(self)
            self._service = service

        def onRegState(self, prm):  # noqa: N802
            logger.info("sip: registration status %s", prm.code)

        def onIncomingCall(self, prm):  # noqa: N802
            self._service._handle_incoming_call(prm.callId)

        def onInstantMessage(self, prm):  # noqa: N802
            try:
                cb = self._service._on_message
                if cb is not None:
                    cb(prm.fromUri, prm.msgBody)
            except Exception:
                logger.exception("sip: onInstantMessage error")


class SIPService:
    """Real pjsua2 SIP service (registered in services["sip"])."""

    def __init__(
        self,
        server: str,
        username: str,
        password: str,
        user_uris: dict[str, str],
        greeting: str = "",
        piper_model: str = "",
        whisper_model: str = "small",
        record_seconds: int = 8,
        port: int = 0,
        answer_settle_seconds: float = 1.2,
        silence_seconds: float = 1.0,
        silence_rms_threshold: int = 500,
    ) -> None:
        """Initialize the SIP service.

        Args:
            server: SIP server host (the Asterisk/registrar).
            username / password: registration credentials.
            user_uris: user_id -> SIP URI (who ARES may call / accept calls from).
            greeting: spoken when answering an incoming call.
            piper_model: path to a Piper `.onnx` voice (TTS). Empty disables TTS.
            whisper_model: faster-whisper model size for call STT.
            record_seconds: HARD CAP on one in-call utterance (§7.5). Endpointing
                is silence-driven (see `silence_seconds`); this is only the safety
                ceiling that ends recording if the caller never stops talking (or
                never starts). Not a fixed wait — a normal utterance ends ~
                `silence_seconds` after the caller falls quiet.
            port: local SIP UDP port (0 = any).
            answer_settle_seconds: pause after an outbound call's media goes
                active, before speaking. A mobile softphone over ZeroTier needs a
                moment to prime its audio output / jitter buffer after answering;
                speaking immediately loses the opening — the whole message if it
                is short — so the callee hears silence.
            silence_seconds: end an in-call utterance after this much *continuous*
                trailing silence, once speech has been heard (spec §7.5's "record
                until silence" — the spec baseline is 700 ms). This is the reply
                latency the caller feels after they stop talking.
            silence_rms_threshold: PCM RMS level (16-bit, 0..32767) below which a
                frame counts as silence. Raise it in a noisy room, lower it if
                quiet speech is being clipped.

        Raises:
            RuntimeError: if the `sip` extra (pjsua2) is not installed.
        """
        if not _HAVE_PJSUA2:
            raise RuntimeError(
                "pjsua2 not installed; pip install -e '.[sip]' (needs system PJSIP)"
            )

        self.server = server
        self.username = username
        self.password = password
        self.user_uris = user_uris
        self.greeting = greeting
        self.piper_model = piper_model
        self.whisper_model = whisper_model
        self.record_seconds = record_seconds
        self.port = port
        self.answer_settle_seconds = answer_settle_seconds
        self.silence_seconds = silence_seconds
        self.silence_rms_threshold = silence_rms_threshold

        self._on_call: Callable[[str], None] | None = None
        self._on_message: Callable[[str, str], None] | None = None

        self._ep = None
        self._acc = None
        self._active_call = None
        self._stt = None  # lazily-built WhisperSTT
        # All pjsua2 commands run on this single registered thread.
        self._exec = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pjsua2"
        )
        self._thread_registered = False

    # ---- pjsua2 thread plumbing ---------------------------------------------

    def _register_thread(self) -> None:
        if self._ep is not None and not self._thread_registered:
            with contextlib.suppress(Exception):
                self._ep.libRegisterThread("ares-pjsua2-exec")
            self._thread_registered = True

    async def _in_pjsua2(self, fn, *args):
        """Run a blocking pjsua2 op on the dedicated (registered) thread."""
        loop = asyncio.get_running_loop()

        def _wrapped():
            self._register_thread()
            return fn(*args)

        return await loop.run_in_executor(self._exec, _wrapped)

    # ---- registration -------------------------------------------------------

    async def register(self) -> None:
        """Create the endpoint (null audio device — headless) and register."""
        await self._in_pjsua2(self._register_blocking)

    def _register_blocking(self) -> None:
        ep = pj.Endpoint()
        ep.libCreate()
        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.threadCnt = 1
        ep_cfg.logConfig.level = 3
        ep.libInit(ep_cfg)
        tcfg = pj.TransportConfig()
        tcfg.port = self.port
        ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)
        ep.libStart()
        # WAV player/recorder ports only — never touch a real sound card.
        ep.audDevManager().setNullDev()
        self._ep = ep

        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = f"sip:{self.username}@{self.server}"
        acc_cfg.regConfig.registrarUri = f"sip:{self.server}"
        acc_cfg.sipConfig.authCreds.append(
            pj.AuthCredInfo("digest", "*", self.username, 0, self.password)
        )
        acc = _Account(self)
        acc.create(acc_cfg)
        self._acc = acc
        logger.info("sip: endpoint up, registering %s@%s", self.username, self.server)

    # ---- outbound MESSAGE ---------------------------------------------------

    async def send_message(self, uri: str, text: str) -> bool:
        """Send a pager-mode SIP MESSAGE to `uri`."""
        try:
            return await self._in_pjsua2(self._send_message_blocking, uri, text)
        except Exception:
            logger.exception("sip: send_message to %s failed", uri)
            return False

    def _send_message_blocking(self, uri: str, text: str) -> bool:
        buddy = pj.Buddy()
        cfg = pj.BuddyConfig()
        cfg.uri = uri
        cfg.subscribe = False
        buddy.create(self._acc, cfg)
        prm = pj.SendInstantMessageParam()
        prm.content = text
        buddy.sendInstantMessage(prm)
        logger.info("sip: MESSAGE -> %s", uri)
        return True

    # ---- calls --------------------------------------------------------------

    def on_incoming_call(self, cb: Callable[[str], None]) -> None:
        """Register the source's incoming-call callback (from_uri)."""
        self._on_call = cb

    def on_incoming_message(self, cb: Callable[[str, str], None]) -> None:
        """Register the source's incoming-message callback (from_uri, text)."""
        self._on_message = cb

    def has_active_call(self) -> bool:
        """True if a call is currently up."""
        return self._active_call is not None

    def _handle_incoming_call(self, call_id) -> None:
        """pjsua2 thread: answer the call, mark it active, notify the source."""
        try:
            call = _Call(self._acc, self, call_id)
            info = call.getInfo()
            from_uri = info.remoteUri
            allowed = set(self.user_uris.values())
            if from_uri not in allowed and not any(
                a in from_uri for a in allowed
            ):
                logger.warning("sip: rejecting call from %s", from_uri)
                prm = pj.CallOpParam()
                prm.statusCode = pj.PJSIP_SC_DECLINE
                call.hangup(prm)
                return
            prm = pj.CallOpParam()
            prm.statusCode = pj.PJSIP_SC_OK
            call.answer(prm)
            self._active_call = call
            logger.info("sip: answered call from %s", from_uri)
            if self._on_call is not None:
                self._on_call(from_uri)
        except Exception:
            logger.exception("sip: error handling incoming call")

    def _on_call_ended(self, call) -> None:
        """pjsua2 thread: clear the active call when it disconnects."""
        if call is self._active_call:
            self._active_call = None
            logger.info("sip: call ended")

    async def call_and_speak(self, uri: str, text: str, listen: bool) -> None:
        """Place an outbound call and speak `text` into it (Piper WAV)."""
        wav = await self._synth(text) if text else None
        await self._in_pjsua2(self._call_and_speak_blocking, uri, wav)

    def _call_and_speak_blocking(self, uri: str, wav: str | None) -> None:
        call = _Call(self._acc, self)
        prm = pj.CallOpParam(True)
        call.makeCall(uri, prm)
        self._active_call = call
        if call.media_ready.wait(timeout=30) and not call.disconnected.is_set():
            # Let the callee's audio path settle before speaking (see
            # answer_settle_seconds) — otherwise a short message is lost and the
            # callee just hears silence.
            if self.answer_settle_seconds > 0:
                time.sleep(self.answer_settle_seconds)
            if wav and not call.disconnected.is_set():
                self._play_wav_blocking(call, wav)

    async def speak_into_call(self, text: str) -> bool:
        """Speak `text` into the currently-active call."""
        if not self.has_active_call():
            logger.warning("sip: no active call to speak into")
            return False
        wav = await self._synth(text)
        if not wav:
            return False
        try:
            await self._in_pjsua2(self._play_wav_blocking, self._active_call, wav)
            return True
        except Exception:
            logger.exception("sip: speak_into_call failed")
            return False

    def _play_wav_blocking(self, call, wav: str) -> None:
        """Play a WAV into the call's audio media, blocking for its duration."""
        media = call.audio_media()
        if media is None:
            logger.warning("sip: no active audio media to play into")
            return
        player = pj.AudioMediaPlayer()
        player.createPlayer(wav, pj.PJMEDIA_FILE_NO_LOOP)
        player.startTransmit(media)
        time.sleep(_wav_duration(wav) + 0.4)
        with contextlib.suppress(Exception):
            player.stopTransmit(media)

    # ---- in-call STT --------------------------------------------------------

    async def record_utterance(self) -> str | None:
        """Record one utterance from the active call and transcribe it (§7.5)."""
        if not self.has_active_call():
            return None
        wav = str(Path(tempfile.mkdtemp()) / "utt.wav")
        try:
            ok = await self._in_pjsua2(self._record_blocking, self._active_call, wav)
            if not ok:
                return None
            return await asyncio.get_running_loop().run_in_executor(
                None, self._transcribe, wav
            )
        except Exception:
            logger.exception("sip: record_utterance failed")
            return None
        finally:
            with contextlib.suppress(OSError):
                Path(wav).unlink()

    def _record_blocking(self, call, wav: str) -> bool:
        media = call.audio_media()
        if media is None:
            return False
        recorder = pj.AudioMediaRecorder()
        recorder.createRecorder(wav)
        media.startTransmit(recorder)
        try:
            self._wait_for_utterance(call, wav)
        finally:
            with contextlib.suppress(Exception):
                media.stopTransmit(recorder)
            del recorder  # flush + close the WAV
        return Path(wav).exists() and Path(wav).stat().st_size > 44

    def _wait_for_utterance(self, call, wav: str) -> None:
        """Block until the caller has finished speaking (spec §7.5 endpointing).

        Tails the WAV the PJSIP recorder is writing (no live sample streaming —
        we only read the temp file, per §7.5) and measures RMS energy on each
        newly-written slice. Returns after `silence_seconds` of *continuous*
        silence once speech has been heard, or at the `record_seconds` hard cap,
        or immediately if the call drops — whichever comes first. Silence before
        the caller says anything does not count, so an opening pause never ends
        the turn early.
        """
        poll = 0.1
        deadline = time.monotonic() + self.record_seconds
        data_offset: int | None = None
        cursor = 0
        speech_seen = False
        silence_run = 0.0

        while time.monotonic() < deadline:
            if call.disconnected.is_set():
                return
            time.sleep(poll)

            if data_offset is None:
                # The recorder writes the header up front, but the file may be
                # mid-flush; wait for a parseable 'data' chunk before measuring.
                data_offset = _wav_data_offset(wav)
                if data_offset is None:
                    continue
                cursor = data_offset

            try:
                size = Path(wav).stat().st_size
            except OSError:
                continue

            level = 0.0
            if size > cursor:
                with open(wav, "rb") as fh:
                    fh.seek(cursor)
                    chunk = fh.read(size - cursor)
                n = len(chunk) - (len(chunk) % 2)  # whole 16-bit samples only
                if n:
                    cursor += n
                    level = _rms_int16(chunk[:n])

            if level >= self.silence_rms_threshold:
                speech_seen = True
                silence_run = 0.0
            elif speech_seen:
                silence_run += poll
                if silence_run >= self.silence_seconds:
                    return

    def _transcribe(self, wav: str) -> str | None:
        if self._stt is None:
            from ares.plugins.sources.voice.stt import WhisperSTT

            self._stt = WhisperSTT(model_size=self.whisper_model)
        text = self._stt.transcribe(wav)
        text = (text or "").strip()
        return text or None

    # ---- TTS ----------------------------------------------------------------

    async def _synth(self, text: str) -> str | None:
        """Piper-synthesize `text` to a temp WAV; None if TTS is unavailable."""
        if not self.piper_model:
            logger.warning("sip: no piper_model configured; cannot speak")
            return None
        wav = str(Path(tempfile.mkdtemp()) / "tts.wav")
        try:
            proc = await asyncio.create_subprocess_exec(
                "piper", "--model", self.piper_model, "--output_file", wav,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("sip: piper binary not found; cannot speak")
            return None
        _, stderr = await proc.communicate(text.encode())
        if proc.returncode != 0 or not Path(wav).exists():
            logger.error("sip: piper failed: %s", stderr.decode(errors="replace"))
            return None
        return wav

    # ---- teardown -----------------------------------------------------------

    async def aclose(self) -> None:
        """Hang up and destroy the endpoint."""
        try:
            await self._in_pjsua2(self._close_blocking)
        except Exception:
            logger.exception("sip: error during aclose")
        finally:
            self._exec.shutdown(wait=False)

    def _close_blocking(self) -> None:
        if self._ep is not None:
            with contextlib.suppress(Exception):
                self._ep.hangupAllCalls()
            with contextlib.suppress(Exception):
                if self._acc is not None:
                    self._acc.shutdown()
            with contextlib.suppress(Exception):
                self._ep.libDestroy()
        self._active_call = None
        self._acc = None
        self._ep = None
        logger.info("sip: endpoint closed")


def _wav_duration(path: str) -> float:
    """Duration of a WAV file in seconds (0.0 on error)."""
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return frames / float(rate)
    except Exception:
        return 0.0


def _rms_int16(raw: bytes) -> float:
    """RMS level of little-endian signed 16-bit PCM (0.0 if empty).

    Stdlib only (no numpy/audioop, so it works on any Python and without the
    voice extra). WAV PCM is little-endian; byteswap on a big-endian host.
    """
    n = len(raw) - (len(raw) % 2)
    if n < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(raw[:n])
    if sys.byteorder == "big":
        samples.byteswap()
    total = 0
    for s in samples:
        total += s * s
    return (total / len(samples)) ** 0.5


def _wav_data_offset(path: str) -> int | None:
    """Byte offset of the PCM payload in a (possibly still-growing) WAV file.

    Walks the RIFF chunk list to find `data` and returns the offset just past
    its 8-byte header. The recorder writes a placeholder `data` length that is
    only finalised on close, but the header's *position* is fixed from the
    start, so this is stable mid-recording. Returns None if the header is not
    yet complete/parseable.
    """
    try:
        with open(path, "rb") as fh:
            header = fh.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            pos = 12
            while True:
                fh.seek(pos)
                chunk_header = fh.read(8)
                if len(chunk_header) < 8:
                    return None
                chunk_id = chunk_header[:4]
                chunk_size = int.from_bytes(chunk_header[4:8], "little")
                if chunk_id == b"data":
                    return pos + 8
                pos += 8 + chunk_size + (chunk_size & 1)  # chunks are word-aligned
    except OSError:
        return None
