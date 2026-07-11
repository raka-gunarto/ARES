"""Test SIP configuration, imports, and config validation.

This is an IMPORT-LEVEL + CONFIG VALIDATION ONLY test suite. No real SIP
functionality is exercised. Tests verify that SIP modules can be imported
without pjsua2 installed, and that configuration validation works correctly
(spec §10 M8 acceptance).

Tests cover:
1. Imports work without pjsua2 extra installed.
2. SIPService raises RuntimeError when pjsua2 is absent.
3. SIPSource config validation (server and username required).
4. Channel type attributes (ChannelType.SIP_MESSAGE, ChannelType.SIP_CALL).
5. Channel delivery with fake service.
6. COMMS_TOOLS validation.
7. Config shape mapping.
"""

from __future__ import annotations

import pytest
import ares.plugins.sip.client as sip_client
from ares.core.config import ConfigError
from ares.core.event import EventBus, Priority
from ares.core.channel import ChannelType
from ares.core.tool import ToolResult
from ares.plugins.sip.source import SIPSource
from ares.plugins.channels.sip_message import SIPMessageChannel
from ares.plugins.channels.sip_call import SIPCallChannel
from ares.plugins.tools.comms_tools import COMMS_TOOLS


# ============================================================================
# 1. Test Imports Clean Without Extra
# ============================================================================


def test_sipservice_importable():
    """SIPService class is importable."""
    from ares.plugins.sip.client import SIPService

    assert SIPService is not None
    assert callable(SIPService)


def test_sipsource_importable():
    """SIPSource class is importable."""
    from ares.plugins.sip.source import SIPSource

    assert SIPSource is not None
    assert callable(SIPSource)


def test_sipmessagechannel_importable():
    """SIPMessageChannel class is importable."""
    from ares.plugins.channels.sip_message import SIPMessageChannel

    assert SIPMessageChannel is not None
    assert callable(SIPMessageChannel)


def test_sipcallchannel_importable():
    """SIPCallChannel class is importable."""
    from ares.plugins.channels.sip_call import SIPCallChannel

    assert SIPCallChannel is not None
    assert callable(SIPCallChannel)


def test_comms_tools_importable():
    """COMMS_TOOLS list is importable."""
    from ares.plugins.tools.comms_tools import COMMS_TOOLS

    assert COMMS_TOOLS is not None
    assert isinstance(COMMS_TOOLS, list)


# ============================================================================
# 2. Test SIPService Raises Without pjsua2
# ============================================================================


def test_sipservice_raises_without_pjsua2():
    """
    SIPService.__init__ raises RuntimeError when pjsua2 is absent.

    Guard: skip this test if pjsua2 is present (sip extra installed).
    """
    if sip_client._HAVE_PJSUA2:
        pytest.skip("pjsua2 is installed; test skipped")

    from ares.plugins.sip.client import SIPService

    with pytest.raises(RuntimeError) as exc_info:
        SIPService("asterisk.local", "ares", "pw", {"primary": "sip:me@x"})

    assert "pjsua2" in str(exc_info.value).lower() or "sip" in str(exc_info.value).lower()


# ============================================================================
# 3. Test SIPSource Config Validation (spec §7)
# ============================================================================


@pytest.fixture
def bus():
    """Provide an EventBus for tests."""
    return EventBus()


def test_sipsource_constructs_with_valid_config(bus):
    """SIPSource constructs when server and username are present."""
    config = {"server": "asterisk.local", "username": "ares"}
    source = SIPSource(bus, config, service=None)

    assert source is not None
    assert source.name == "sip"


def test_sipsource_raises_missing_server(bus):
    """SIPSource raises ConfigError when server is missing."""
    config = {"username": "ares"}

    with pytest.raises(ConfigError) as exc_info:
        SIPSource(bus, config, service=None)

    assert "server" in str(exc_info.value).lower()


def test_sipsource_raises_missing_username(bus):
    """SIPSource raises ConfigError when username is missing."""
    config = {"server": "asterisk.local"}

    with pytest.raises(ConfigError) as exc_info:
        SIPSource(bus, config, service=None)

    assert "username" in str(exc_info.value).lower()


def test_sipsource_name_is_sip():
    """SIPSource.name class attribute is 'sip'."""
    assert SIPSource.name == "sip"


# ============================================================================
# 4. Test Channel Types
# ============================================================================


def test_sipmessagechannel_type():
    """SIPMessageChannel.type == ChannelType.SIP_MESSAGE."""
    assert SIPMessageChannel.type == ChannelType.SIP_MESSAGE


def test_sipcallchannel_type():
    """SIPCallChannel.type == ChannelType.SIP_CALL."""
    assert SIPCallChannel.type == ChannelType.SIP_CALL


# ============================================================================
# 5. Test Channel Delivery with Fake Service
# ============================================================================


class FakeSIPService:
    """Fake SIPService for testing channel delivery."""

    def __init__(self):
        self.user_uris = {"primary": "sip:me@x"}

    async def send_message(self, uri: str, text: str) -> bool:
        """Fake send_message: return True."""
        return True

    async def speak_into_call(self, text: str) -> bool:
        """Fake speak_into_call: return False (no active call)."""
        return False


@pytest.mark.asyncio
async def test_sipmessagechannel_deliver_success():
    """
    SIPMessageChannel.deliver("primary", "hi", None) returns True with fake
    service that returns True from send_message.
    """
    service = FakeSIPService()
    channel = SIPMessageChannel(service)

    result = await channel.deliver("primary", "hi", None)

    assert result is True


@pytest.mark.asyncio
async def test_sipmessagechannel_deliver_unknown_user():
    """
    SIPMessageChannel.deliver("unknown", "hi", None) returns False when user
    is not in service.user_uris.
    """
    service = FakeSIPService()
    channel = SIPMessageChannel(service)

    result = await channel.deliver("unknown", "hi", None)

    assert result is False


@pytest.mark.asyncio
async def test_sipcallchannel_deliver_no_active_call():
    """
    SIPCallChannel.deliver("primary", "hi", None) returns False because no
    active call exists (speak_into_call returns False).
    """
    service = FakeSIPService()
    channel = SIPCallChannel(service)

    result = await channel.deliver("primary", "hi", None)

    assert result is False


# ============================================================================
# 6. Test COMMS_TOOLS
# ============================================================================


def test_comms_tools_length():
    """COMMS_TOOLS has exactly 2 tools."""
    assert len(COMMS_TOOLS) == 2


def test_comms_tools_all_non_core():
    """All tools in COMMS_TOOLS have core=False."""
    for tool in COMMS_TOOLS:
        assert tool.core is False


class MinimalToolContext:
    """Minimal ToolContext for testing tool behavior without services."""

    def __init__(self, services=None, user_id="primary"):
        self.services = services or {}
        self.user_id = user_id


@pytest.mark.asyncio
async def test_comms_tools_no_sip_service():
    """
    Both COMMS_TOOLS return ToolResult(False, "SIP is not configured.") when
    services dict is empty.
    """
    ctx = MinimalToolContext(services={})

    for tool in COMMS_TOOLS:
        result = await tool.run(ctx, message="test")

        assert result.ok is False
        assert "SIP is not configured" in result.content


# ============================================================================
# 7. Test Config Shape Mapping
# ============================================================================


def test_sipsource_config_shape_valid(bus):
    """
    A spec §8-style SIP config dict with enabled, server, username, password,
    greeting maps cleanly to SIPSource construction (no ConfigError).
    """
    config = {
        "enabled": True,
        "server": "asterisk.local",
        "username": "ares",
        "password": "x",
        "greeting": "ARES. Go ahead.",
    }

    # Should not raise ConfigError
    source = SIPSource(bus, config, service=None)

    assert source is not None
    assert source.config["server"] == "asterisk.local"
    assert source.config["username"] == "ares"
