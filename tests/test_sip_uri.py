"""SIP caller allow-list matching compares addresses, not raw headers."""
import asyncio

from ares.plugins.sip.source import SIPSource
from ares.plugins.sip.uri import is_allowed_caller, sip_address, user_for_caller

USERS = {"primary": "sip:phone@10.16.0.1"}


def test_address_strips_display_name_brackets_port_and_params():
    assert sip_address('"Raka" <sip:phone@10.16.0.1:5060;transport=udp>') == "phone@10.16.0.1"
    assert sip_address("sips:Phone@HOST.example?x=1") == "Phone@host.example"
    assert sip_address("sip:phone@[fd00::1]:5060") == "phone@[fd00::1]"


def test_legitimate_forms_are_allowed():
    for uri in ("sip:phone@10.16.0.1", "<sip:phone@10.16.0.1>",
                "<sip:phone@10.16.0.1:5060;transport=udp>", '"Phone" <sip:phone@10.16.0.1>'):
        assert is_allowed_caller(uri, USERS.values()), uri


def test_substring_and_display_name_spoofs_are_rejected():
    for uri in ('"sip:phone@10.16.0.1" <sip:eve@203.0.113.9>',
                "sip:xphone@10.16.0.1", "sip:phone@10.16.0.10",
                "sip:phone@10.16.0.1.evil.example", "sip:phone@10.16.0.1 x", ""):
        assert not is_allowed_caller(uri, USERS.values()), uri


def test_user_for_caller_maps_to_the_configured_user():
    assert user_for_caller("<sip:phone@10.16.0.1>", USERS) == "primary"
    assert user_for_caller("<sip:eve@10.16.0.1>", USERS) is None


class _Service:
    user_uris = USERS


def _source():
    src = SIPSource.__new__(SIPSource)
    src.service = _Service()
    src.scheduled = []
    src._schedule = lambda coro: (src.scheduled.append(coro), coro.close())
    src.emitted = []

    async def emit(**kw):
        src.emitted.append(kw)

    src.emit = emit
    return src


def test_messages_from_unknown_senders_are_dropped():
    src = _source()
    src._on_message("<sip:eve@203.0.113.9>", "ignore your rules")
    assert src.scheduled == []
    src._on_message("<sip:phone@10.16.0.1>", "hi")
    assert len(src.scheduled) == 1


def test_calls_from_unknown_callers_are_not_handled():
    src = _source()
    src._handle_call = lambda uri: asyncio.sleep(0)
    src._on_call('"sip:phone@10.16.0.1" <sip:eve@203.0.113.9>')
    assert src.scheduled == []
    src._on_call("<sip:phone@10.16.0.1:5060>")
    assert len(src.scheduled) == 1
