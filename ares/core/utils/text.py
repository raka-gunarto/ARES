from __future__ import annotations

import re


def tokenize(s: str) -> list[str]:
    """Tokenize a string into lowercase words."""
    return re.findall(r"[a-z0-9]+", s.lower())


def unspoken_final(final_text: str, spoken_texts: list[str]) -> bool:
    """True when `final_text` is a real answer the agent wrote as its final
    message but never delivered through `speak`.

    Guards a specific failure (spec §4.10 step 8): the model calls `speak` once to
    acknowledge — "On it, checking now" — then writes the actual answer as its
    final assistant message, which the delivery step drops because it already
    spoke. That answer should be delivered. The opposite shape must NOT be: the
    model spoke the full answer and its final message is only a shorter
    note-to-self ("Declined and explained — nothing further to do").

    Heuristic: worth delivering when the final is at least as long as everything
    already spoken (an answer after a brief ack, not a summary of a full reply)
    and is not merely a restatement of it (its words are not largely a subset of
    what was spoken).
    """
    final = (final_text or "").strip()
    if not final:
        return False
    spoken = " ".join(t for t in spoken_texts if t).strip()
    if len(final) < len(spoken):
        return False
    ftok = set(tokenize(final))
    if len(ftok) < 4:
        return False  # too short to be a dropped answer (filler like "done"/"ok")
    stok = set(tokenize(spoken))
    if not stok:
        return True
    return len(ftok & stok) / len(ftok) < 0.8
