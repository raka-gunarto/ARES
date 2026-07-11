from __future__ import annotations

import re


def tokenize(s: str) -> list[str]:
    """Tokenize a string into lowercase words."""
    return re.findall(r"[a-z0-9]+", s.lower())
