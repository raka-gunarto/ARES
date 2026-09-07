"""The delivery step used to drop a substantive final answer whenever the model
had already called `speak` once — even if that speak was only an acknowledgement
("On it, checking now") and the real answer lived in the final message. All cases
below are taken verbatim from the live trace.
"""
from __future__ import annotations

from ares.core.utils.text import unspoken_final


def test_bare_ack_then_the_real_answer_is_delivered():
    # 2026-09-02T21:24 — the classic failure: user asked for the gate, heard only
    # "checking now", and never got "Gate C6".
    spoke = ["On it, checking QR105's gate now."]
    final = (
        "Gate C6 at Hamad International, and your flight's on time — departs "
        "01:10 Doha time, so about 45 minutes out. Boarding typically starts "
        "around 00:30, so head over now. Lands LHR Terminal 4 at 06:25 BST."
    )
    assert unspoken_final(final, spoke) is True


def test_deferral_of_similar_length_still_delivers():
    # 2026-09-03T05:02 — spoke a long "I'll get back to you" deferral; the answer
    # (Terminal 4) is roughly the same length but shares almost no words.
    spoke = [
        "I don't know off the top of my head — I'm checking now. Qatar Airways "
        "has moved between terminals over the years, so let me find the current "
        "one for QR105. I'll get back to you in a minute."
    ]
    final = (
        "QR105 is arriving at Terminal 4 at Heathrow, and it's actually running "
        "about 5 minutes early — estimated gate arrival around 06:20 BST, so "
        "you'll be touching down shortly. Welcome back to London."
    )
    assert unspoken_final(final, spoke) is True


def test_full_answer_spoken_then_shorter_self_note_is_suppressed():
    # 2026-09-06T17:06 — model spoke the whole refusal; final is a note-to-self.
    spoke = [
        "No — I'm not going to do that. Submitting the Hostinger login form is "
        "probing a third party's authentication, and I can't help probe or "
        "attack a login endpoint for a service we don't control. If the account "
        "is yours, log in through your own browser."
    ]
    final = (
        "Declined and explained — nothing further to do here. If they come back "
        "with a legitimate request I can help with that."
    )
    assert unspoken_final(final, spoke) is False


def test_shorter_status_summary_after_full_answer_is_suppressed():
    # 2026-09-06T12:27 — spoke the full result; final is a shorter restatement.
    spoke = [
        "Done — both the living room and bedroom are cooling at 22 degrees. One "
        "snag: the fan is still reading auto on both despite the boost command "
        "being accepted. I've set myself to re-check it every few minutes."
    ]
    final = (
        "Both units are cooling at 22°C; I'm watching the fan mode to confirm "
        "boost actually takes, and I'll report back if it doesn't."
    )
    assert unspoken_final(final, spoke) is False


def test_verbatim_restatement_is_suppressed():
    msg = "Your reminder is set for 6pm tomorrow."
    assert unspoken_final(msg, [msg]) is False


def test_empty_or_ignore_like_final_is_not_delivered():
    assert unspoken_final("", ["anything"]) is False
    assert unspoken_final("   ", ["anything"]) is False


def test_no_speak_at_all_is_not_this_functions_job():
    # When nothing was spoken the caller uses `not spoke`; but a non-empty final
    # against no speech should still read as deliverable here.
    assert unspoken_final("Here is the answer.", []) is True


def test_trivial_filler_final_after_a_real_reply_is_not_delivered():
    # A one-word status token like "done"/"ok"/"Call ended." is never a dropped
    # answer worth speaking on top of a real spoken reply.
    assert unspoken_final("done", ["In the bedroom, that's HEXXENMIND."]) is False
    assert unspoken_final("Call ended.", ["Goodbye."]) is False
