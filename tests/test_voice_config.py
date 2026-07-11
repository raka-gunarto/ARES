"""Import-level and config-validation tests for voice plugin (M7).

Per spec §10, M7 acceptance is IMPORT-LEVEL + CONFIG VALIDATION ONLY (no real
audio hardware in this environment). The `voice` extra is not installed; these
tests verify:
- Modules import cleanly without the extra
- Heavy wrappers raise RuntimeError when instantiated without the extra
- Config validation works correctly
"""

from __future__ import annotations

import pytest

from ares.core.channel import ChannelType
from ares.core.config import ConfigError
from ares.core.event import EventBus, Priority
from ares.plugins.channels.voice_tts import VoiceTTSChannel
from ares.plugins.sources.voice.intent import IntentFilter
from ares.plugins.sources.voice.source import VoiceSource
from ares.plugins.sources.voice.stt import WhisperSTT
from ares.plugins.sources.voice.stt import _HAVE_STT as stt_available
from ares.plugins.sources.voice.vad import SileroVAD
from ares.plugins.sources.voice.vad import _HAVE_VAD as vad_available


class TestImports:
    """Test that voice modules import cleanly without the voice extra."""

    def test_vad_module_imports(self) -> None:
        """Test that vad.py imports and SileroVAD is available."""
        from ares.plugins.sources.voice import vad  # noqa: F401

        assert hasattr(vad, "SileroVAD")
        assert hasattr(vad, "_HAVE_VAD")

    def test_stt_module_imports(self) -> None:
        """Test that stt.py imports and WhisperSTT is available."""
        from ares.plugins.sources.voice import stt  # noqa: F401

        assert hasattr(stt, "WhisperSTT")
        assert hasattr(stt, "_HAVE_STT")

    def test_intent_module_imports(self) -> None:
        """Test that intent.py imports and IntentFilter is available."""
        from ares.plugins.sources.voice import intent  # noqa: F401

        assert hasattr(intent, "IntentFilter")

    def test_source_module_imports(self) -> None:
        """Test that source.py imports and VoiceSource is available."""
        from ares.plugins.sources.voice import source  # noqa: F401

        assert hasattr(source, "VoiceSource")

    def test_voice_tts_channel_imports(self) -> None:
        """Test that voice_tts.py imports and VoiceTTSChannel is available."""
        from ares.plugins.channels import voice_tts  # noqa: F401

        assert hasattr(voice_tts, "VoiceTTSChannel")


class TestHeavyWrappersRaise:
    """Test that heavy wrappers raise RuntimeError when the voice extra is missing."""

    @pytest.mark.skipif(vad_available, reason="voice extra is installed")
    def test_silero_vad_raises_without_extra(self) -> None:
        """SileroVAD() raises RuntimeError if silero-vad is not installed."""
        with pytest.raises(RuntimeError, match="silero-vad and numpy are not installed"):
            SileroVAD()

    @pytest.mark.skipif(stt_available, reason="voice extra is installed")
    def test_whisper_stt_raises_without_extra(self) -> None:
        """WhisperSTT() raises RuntimeError if faster-whisper is not installed."""
        with pytest.raises(RuntimeError, match="faster-whisper is not installed"):
            WhisperSTT()


class TestIntentFilterConfig:
    """Test IntentFilter config validation."""

    def test_intent_filter_wake_word_strategy(self) -> None:
        """IntentFilter('wake_word', 'hey_ares') constructs without error."""
        f = IntentFilter("wake_word", "hey_ares")
        assert f.strategy == "wake_word"
        assert f.wake_word == "hey_ares"
        assert f.llm is None

    def test_intent_filter_llm_strategy_with_none_llm(self) -> None:
        """IntentFilter('llm', 'hey_ares', llm=None) constructs without error."""
        f = IntentFilter("llm", "hey_ares", llm=None)
        assert f.strategy == "llm"
        assert f.wake_word == "hey_ares"
        assert f.llm is None

    def test_intent_filter_hybrid_strategy(self) -> None:
        """IntentFilter('hybrid', 'hey_ares', llm=None) constructs without error."""
        f = IntentFilter("hybrid", "hey_ares", llm=None)
        assert f.strategy == "hybrid"
        assert f.wake_word == "hey_ares"
        assert f.llm is None

    def test_intent_filter_bogus_strategy_raises(self) -> None:
        """IntentFilter('bogus_strategy', 'x') raises ValueError."""
        with pytest.raises(ValueError, match="Unknown intent strategy"):
            IntentFilter("bogus_strategy", "x")

    def test_intent_filter_invalid_strategy_name(self) -> None:
        """IntentFilter with an invalid strategy name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown intent strategy"):
            IntentFilter("invalid", "wake_word_name")


class TestVoiceSourceConfig:
    """Test VoiceSource config validation."""

    def test_voice_source_constructs_with_valid_config(self) -> None:
        """VoiceSource constructs with valid room and input_device."""
        bus = EventBus()
        source = VoiceSource(
            bus,
            {},
            room="kitchen",
            input_device="USB Audio 1",
            vad=None,
            stt=None,
            intent=None,
        )
        assert source.room == "kitchen"
        assert source.input_device == "USB Audio 1"
        assert source.name == "voice"

    def test_voice_source_empty_room_raises_config_error(self) -> None:
        """VoiceSource with empty room raises ConfigError."""
        bus = EventBus()
        with pytest.raises(ConfigError, match="voice: room is required"):
            VoiceSource(
                bus,
                {},
                room="",
                input_device="USB Audio 1",
                vad=None,
                stt=None,
                intent=None,
            )

    def test_voice_source_none_room_raises_config_error(self) -> None:
        """VoiceSource with None room raises ConfigError."""
        bus = EventBus()
        with pytest.raises(ConfigError, match="voice: room is required"):
            VoiceSource(
                bus,
                {},
                room=None,
                input_device="USB Audio 1",
                vad=None,
                stt=None,
                intent=None,
            )

    def test_voice_source_none_input_device_raises_config_error(self) -> None:
        """VoiceSource with None input_device raises ConfigError."""
        bus = EventBus()
        with pytest.raises(ConfigError, match="input_device is required"):
            VoiceSource(
                bus,
                {},
                room="kitchen",
                input_device=None,
                vad=None,
                stt=None,
                intent=None,
            )

    def test_voice_source_name_attribute(self) -> None:
        """VoiceSource.name is 'voice'."""
        bus = EventBus()
        source = VoiceSource(
            bus,
            {},
            room="kitchen",
            input_device="x",
            vad=None,
            stt=None,
            intent=None,
        )
        assert source.name == "voice"


class TestVoiceTTSChannel:
    """Test VoiceTTSChannel config and construction."""

    def test_voice_tts_channel_type(self) -> None:
        """VoiceTTSChannel.type is ChannelType.VOICE."""
        channel = VoiceTTSChannel(
            rooms={"living_room": {"output_device": "x"}},
            default_room="living_room",
            piper_model="en_GB-alan-medium",
        )
        assert channel.type == ChannelType.VOICE

    def test_voice_tts_channel_constructs(self) -> None:
        """VoiceTTSChannel constructs with valid config."""
        rooms = {"living_room": {"output_device": "Speaker 1"}}
        channel = VoiceTTSChannel(
            rooms=rooms,
            default_room="living_room",
            piper_model="en_GB-alan-medium",
        )
        assert channel.default_room == "living_room"
        assert channel.piper_model == "en_GB-alan-medium"
        assert channel.rooms == rooms

    def test_voice_tts_channel_with_multiple_rooms(self) -> None:
        """VoiceTTSChannel constructs with multiple rooms."""
        rooms = {
            "kitchen": {"output_device": "Speaker 1"},
            "living_room": {"output_device": "Speaker 2"},
        }
        channel = VoiceTTSChannel(
            rooms=rooms,
            default_room="living_room",
            piper_model="en_GB-alan-medium",
        )
        assert "kitchen" in channel.rooms
        assert "living_room" in channel.rooms

    @pytest.mark.asyncio
    async def test_voice_tts_channel_deliver_returns_false_gracefully(self) -> None:
        """VoiceTTSChannel.deliver returns False gracefully without audio libs."""
        from ares.core.session import SessionManager

        channel = VoiceTTSChannel(
            rooms={"living_room": {"output_device": "x"}},
            default_room="living_room",
            piper_model="en_GB-alan-medium",
        )

        mgr = SessionManager()
        session = mgr.touch("primary", ChannelType.VOICE, "living_room")

        # deliver should return False gracefully when audio/piper are missing
        result = await channel.deliver("primary", "hello world", session)
        assert result is False


class TestVoiceConfigShape:
    """Test that a complete voice config maps to plugin constructors."""

    def test_voice_config_to_voice_source(self) -> None:
        """A full voice config dict can construct a VoiceSource for a room."""
        config = {
            "enabled": True,
            "whisper_model": "small",
            "intent_strategy": "hybrid",
            "wake_word": "hey_ares",
            "default_room": "living_room",
            "rooms": {
                "kitchen": {
                    "input_device": "USB Audio 1",
                    "output_device": "USB Audio 1",
                },
                "living_room": {
                    "input_device": "USB Audio 2",
                    "output_device": "USB Audio 2",
                },
            },
            "piper_model": "en_GB-alan-medium",
        }

        # Extract the kitchen room config and construct a VoiceSource
        kitchen_config = config["rooms"]["kitchen"]
        bus = EventBus()

        source = VoiceSource(
            bus,
            config,
            room="kitchen",
            input_device=kitchen_config["input_device"],
            vad=None,
            stt=None,
            intent=None,
        )

        assert source.room == "kitchen"
        assert source.input_device == "USB Audio 1"

    def test_voice_config_to_voice_tts_channel(self) -> None:
        """A full voice config dict can construct a VoiceTTSChannel."""
        config = {
            "enabled": True,
            "whisper_model": "small",
            "intent_strategy": "hybrid",
            "wake_word": "hey_ares",
            "default_room": "living_room",
            "rooms": {
                "kitchen": {
                    "input_device": "USB Audio 1",
                    "output_device": "USB Audio 1",
                },
                "living_room": {
                    "input_device": "USB Audio 2",
                    "output_device": "USB Audio 2",
                },
            },
            "piper_model": "en_GB-alan-medium",
        }

        # Construct a VoiceTTSChannel from the config
        channel = VoiceTTSChannel(
            rooms=config["rooms"],
            default_room=config["default_room"],
            piper_model=config["piper_model"],
        )

        assert channel.default_room == "living_room"
        assert channel.piper_model == "en_GB-alan-medium"
        assert "kitchen" in channel.rooms
        assert "living_room" in channel.rooms

    def test_voice_config_intent_filter_construction(self) -> None:
        """A full voice config can construct an IntentFilter."""
        config = {
            "enabled": True,
            "whisper_model": "small",
            "intent_strategy": "hybrid",
            "wake_word": "hey_ares",
            "default_room": "living_room",
            "rooms": {
                "kitchen": {
                    "input_device": "USB Audio 1",
                    "output_device": "USB Audio 1",
                },
            },
            "piper_model": "en_GB-alan-medium",
        }

        # Construct an IntentFilter from the config
        intent = IntentFilter(
            strategy=config["intent_strategy"],
            wake_word=config["wake_word"],
            llm=None,  # No LLM in config; would be injected from core
        )

        assert intent.strategy == "hybrid"
        assert intent.wake_word == "hey_ares"
