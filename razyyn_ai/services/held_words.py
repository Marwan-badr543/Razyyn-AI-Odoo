# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Gathering the answer's words before they are written down.

Standalone on purpose — no Odoo, no network, nothing but the clock — so the
rule it enforces (prose may be held, a milestone may never overtake it) is
testable without an Odoo installation, the same way ``query_guard`` is.
"""

from __future__ import annotations

import time


#: How long the answer's words may be held before they are sent on, and how
#: many of them may pile up in the meantime. See HeldWords.
CHUNK_HOLD_SECONDS = 0.25
CHUNK_HOLD_CHARS = 2000


class HeldWords:
    """The answer's words, gathered for a quarter-second before being sent.

    WHY THE WORDS ARE NOT SENT ONE AT A TIME.
        The agent writes its answer a few letters at a time, and each piece
        used to be a row of its own in the event table -- written, committed,
        and then found again by every open chat window's next look. A long
        report is thousands of pieces, so a single answer was thousands of
        writes and thousands of commits on the way out and a growing table to
        search on the way back in. That is the whole reason a busy Odoo slowed
        to a stop while two conversations were being answered, and it is not a
        problem ERPNext has: there the same words go out over a socket and are
        never written down at all.

        Nothing is lost or reordered by gathering them: the chat window joins
        the pieces back together in the order they arrive, so one row holding
        a quarter-second of writing is indistinguishable from twenty rows
        holding the same letters -- except in what it costs.

    WHAT MUST ALWAYS BE SENT FIRST.
        Anything that is NOT part of the answer's prose -- a step starting, the
        checklist changing, the answer finishing, a failure -- has to arrive
        after the words that came before it, or the chat window draws the story
        out of order. So every other event flushes what is being held, and so
        does the end of the turn, however it ends.
    """

    def __init__(self, send):
        self._send = send
        self._text = {}
        self._since = {}

    def hold(self, event: str, text: str) -> None:
        if not text:
            return
        self._text[event] = self._text.get(event, "") + text
        started = self._since.setdefault(event, time.monotonic())
        if (time.monotonic() - started >= CHUNK_HOLD_SECONDS
                or len(self._text[event]) >= CHUNK_HOLD_CHARS):
            self.flush()

    def flush(self) -> None:
        held, self._text, self._since = self._text, {}, {}
        for event, text in held.items():
            if text:
                self._send(event, text)

