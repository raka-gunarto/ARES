"""RULES is one block living in three files; nothing was checking they agree.

Spec §4.11 says the block is "byte-identical in `prompt.py` and
`ARES-SYSTEM-PROMPT.md`" and reproduces it verbatim in the spec itself. That was
an honour-system rule until v1.11 edited all three at once and there was no way
to prove the edit landed everywhere. Drift here is quiet and expensive: the
executable copy is the one in code, so a stale doc copy misleads every future
reader about what the running agent is actually told.
"""
from __future__ import annotations

import pathlib
import re

from ares.core.prompt import RULES

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = ("ARES-SPEC.md", "ARES-SYSTEM-PROMPT.md")

_FENCED = re.compile(r"```\n(--- RULES ---\n.*?)\n```", re.S)


def _blocks(name: str) -> list[str]:
    """Every fenced RULES block in `name`."""
    return _FENCED.findall((REPO / name).read_text(encoding="utf-8"))


def test_each_doc_carries_exactly_one_rules_block() -> None:
    for name in DOCS:
        assert len(_blocks(name)) == 1, f"{name} should carry exactly one RULES block"


def test_doc_copies_are_byte_identical_to_the_code_constant() -> None:
    for name in DOCS:
        assert _blocks(name)[0] == RULES, (
            f"{name}'s RULES block has drifted from ares.core.prompt.RULES; "
            "the code constant is the executable one — update the doc to match."
        )


def test_rules_still_carries_the_delegation_instruction() -> None:
    """v1.11. A capability with no prompt-level trigger goes unused (§20)."""
    assert "spawn_subagent" in RULES
    assert "never something to IGNORE" in RULES
