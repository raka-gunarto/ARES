"""SIP address matching for the caller allow-list (spec §7.5).

An incoming From header carries a display name, angle brackets, a port and
parameters around the address — e.g. ``"Raka" <sip:phone@10.16.0.1:5060;transport=udp>``.
Comparing raw strings (or substrings) is either too strict or unsafe: the
display name is caller-controlled, so ``"sip:phone@10.16.0.1" <sip:eve@evil>``
must not match. Only the address itself — user@host — is compared.
"""
from __future__ import annotations

import re

_BRACKETED = re.compile(r"<([^>]*)>")


def sip_address(uri: str) -> str:
    """Reduce a SIP URI or From header to a lowercase ``user@host`` (or host)."""
    text = (uri or "").strip()
    match = _BRACKETED.search(text)
    if match:
        text = match.group(1)
    elif " " in text:
        return ""  # a display name with no bracketed address: nothing to trust
    text = re.sub(r"^sips?:", "", text, flags=re.IGNORECASE)
    text = re.split(r"[;?]", text, maxsplit=1)[0]
    user, sep, host = text.rpartition("@")
    if host.startswith("["):  # [v6]:port
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    return f"{user}{sep}{host.lower()}"


def is_allowed_caller(from_uri: str, allowed: list[str] | set[str]) -> bool:
    """True if `from_uri`'s address equals one of the configured user URIs."""
    address = sip_address(from_uri)
    return bool(address) and any(address == sip_address(a) for a in allowed)


def user_for_caller(from_uri: str, user_uris: dict[str, str]) -> str | None:
    """The user_id whose configured URI matches `from_uri`, else None."""
    address = sip_address(from_uri)
    for user_id, uri in user_uris.items():
        if address and address == sip_address(uri):
            return user_id
    return None
