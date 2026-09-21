# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""The live progress table: what goes into it, and who takes it out again.

WHY THIS FILE EXISTS
    On ERPNext live progress goes out over a socket and is never written down.
    Odoo has no socket for a page served outside the web client, so every event
    becomes a row -- and a live site was measured writing 41,252 of them in
    twenty minutes, nine in ten of which were the model thinking aloud.

    Two changes follow from that, and both are invisible when they break:

      1. THE MODEL'S SCRATCHPAD IS NEVER WRITTEN. Everything the customer
         actually follows still is.
      2. EACH TURN REMOVES ITS OWN ROWS a minute after it ends, so the table is
         empty between conversations instead of holding hours of a customer's
         ledger traffic for nothing.

    The dangerous half is (2). A sweep that removed "this conversation's
    events" would, for a customer who types their next message quickly, delete
    the NEXT answer's rows while they were still being read -- rarely, and
    unreproducibly. Hence a case for exactly that, below.
"""

import json
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase

from ..models import agent_chat_event as event_model
from ..services import chat_turn_service as turns


class _Stream:
    """Enough of a `requests` streaming response for `_relay` to read."""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, chunk_size=1):
        for line in self._lines:
            yield line.encode("utf-8")


def _sse(event: str, body: dict) -> list:
    return [f"event: {event}", f"data: {json.dumps(body)}", ""]


class _EventsTestCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.events = self.env["razyyn.agent.chat.event"].sudo()
        self.sessions = self.env["razyyn.agent.chat.session"].sudo()
        # START FROM AN EMPTY TABLE, AND SAY SO RATHER THAN ASSUME IT. A real
        # site has thousands of rows in here, and a case written against an
        # empty database counts them into its own totals and fails on the one
        # machine that matters. Rolled back with the rest of the test.
        self.env.flush_all()
        self.env.cr.execute("DELETE FROM razyyn_agent_chat_event")
        self.env.invalidate_all()

    def _session(self, session_id: str):
        return self.sessions.create({
            "session_id": session_id,
            "title": session_id,
            "user_id": self.env.uid,
        })

    def _write(self, session_id: str, count: int = 1, event: str = "agent_message_chunk"):
        rows = self.events.browse()
        for index in range(count):
            rows |= self.events.create({
                "user_id": self.env.uid,
                "session_id": session_id,
                "event": event,
                "payload": json.dumps({"chunk": f"piece {index}"}),
            })
        return rows

    def _rows_of(self, session_id: str):
        return self.events.search([("session_id", "=", session_id)])


class TheTurnSweepsUpAfterItself(_EventsTestCase):

    def test_what_the_turn_wrote_is_gone(self):
        self._write("alpha", 5)
        mark = turns.highest_event_id(self.env, "alpha")

        removed = turns.sweep_events(self.env.cr, "alpha", mark)

        self.assertEqual(removed, 5)
        self.assertFalse(self._rows_of("alpha"))

    def test_another_conversation_is_never_touched(self):
        self._write("alpha", 3)
        self._write("beta", 4)
        mark = turns.highest_event_id(self.env, "alpha")

        turns.sweep_events(self.env.cr, "alpha", mark)

        self.assertFalse(self._rows_of("alpha"))
        self.assertEqual(len(self._rows_of("beta")), 4,
                         "the sweep is scoped to one conversation")

    def test_a_second_turn_in_the_same_chat_keeps_its_rows(self):
        """The trap this whole design exists to avoid.

        The answer lands, the customer immediately types a follow-up, and a
        minute later the FIRST turn's cleanup fires. Scoped to the
        conversation it would delete the second answer's rows while they were
        still being read, and that answer would stop dead mid-sentence.
        """
        self._write("alpha", 3)                      # the first turn
        mark = turns.highest_event_id(self.env, "alpha")
        second = self._write("alpha", 4)             # the follow-up, still streaming

        removed = turns.sweep_events(self.env.cr, "alpha", mark)

        self.assertEqual(removed, 3, "only the first turn's rows")
        self.assertEqual(self._rows_of("alpha"), second,
                         "the turn in progress keeps every row it has written")

    def test_a_ceiling_read_after_the_follow_up_would_have_eaten_it(self):
        """Proves the ceiling has to be read when the turn ENDS, not later."""
        self._write("alpha", 3)
        self._write("alpha", 4)
        too_late = turns.highest_event_id(self.env, "alpha")

        turns.sweep_events(self.env.cr, "alpha", too_late)

        self.assertFalse(self._rows_of("alpha"),
                         "a ceiling read too late takes the follow-up with it")

    def test_a_turn_that_wrote_nothing_schedules_nothing(self):
        with patch.object(turns.threading, "Thread") as thread:
            turns.schedule_event_cleanup(self.env.cr.dbname, "alpha", 0)
        thread.assert_not_called()

    def test_a_turn_that_wrote_something_schedules_a_sweep(self):
        with patch.object(turns.threading, "Thread") as thread:
            turns.schedule_event_cleanup(self.env.cr.dbname, "alpha", 17)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["args"],
                         (self.env.cr.dbname, "alpha", 17))
        self.assertTrue(thread.call_args.kwargs["daemon"],
                        "a sweep must never hold a shutting-down server open")

    def test_the_wait_is_long_enough_to_outlast_a_reconnect(self):
        """Nothing may be deleted while it is still on its way to a browser.

        The stream is reopened about a second after it closes and the reader
        looks twice a second, so the exposure window is a couple of seconds.
        The delay has to clear that with room to spare -- a laptop that slept
        through the end of the answer is the case that needs the margin.
        """
        self.assertGreaterEqual(turns.EVENT_CLEANUP_DELAY_SECONDS, 30)

    def test_the_high_water_mark_of_an_empty_conversation_is_nothing(self):
        self.assertEqual(turns.highest_event_id(self.env, "never-used"), 0)


class TheHourlyBackstop(_EventsTestCase):
    """The cron is for turns that died before they could tidy up."""

    def _age(self, rows, hours):
        """Make rows look `hours` old ON THE CLOCK THE SWEEP READS.

        NOT PostgreSQL's `now()`, and this cost an afternoon. The database
        here answers `now()` in Africa/Cairo while Odoo stores every date in
        UTC, so `now() - 3 hours` wrote a date three hours in the FUTURE of
        the sweep's own clock. Ageing by three did nothing, ageing by five
        worked, and the boundary was the timezone rather than anything about
        the sweep. On a machine set to UTC the same test would have passed and
        the fixture would still have been wrong.

        Flush first, because these rows may still be pending in the ORM and a
        later flush would put the original date back over this one; invalidate
        after, so nothing reads the date from a cache that predates it.
        """
        stale = fields.Datetime.subtract(fields.Datetime.now(), hours=hours)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE razyyn_agent_chat_event SET create_date = %s WHERE id = ANY(%s)",
            (stale, rows.ids),
        )
        self.env.invalidate_all()

    def test_an_abandoned_turn_is_cleared(self):
        self._age(self._write("alpha", 3), 5)

        self.events.cron_prune_delivered_events()

        self.assertFalse(self._rows_of("alpha"))

    def test_a_conversation_being_answered_right_now_is_untouched(self):
        """The heart of it: the backstop goes by AGE, never by conversation.

        A row that has not reached a browser yet is seconds old -- it is read
        within half a second of being written. An hour is not a guess at how
        long a turn takes; it is a margin no in-flight row can reach.
        """
        live = self._write("alpha", 4)          # written moments ago
        self._age(self._write("beta", 2), 9)    # an abandoned turn

        self.events.cron_prune_delivered_events()

        self.assertEqual(self._rows_of("alpha"), live,
                         "a running conversation keeps its events")
        self.assertFalse(self._rows_of("beta"))

    def test_a_long_audit_keeps_the_rows_still_in_flight(self):
        """A three-hour audit outlives the retention, and that is fine.

        Its first minutes were delivered three hours ago; only its newest rows
        are still on their way, and those are exactly the ones an age cutoff
        can never catch.
        """
        delivered = self._write("alpha", 5)     # the early part of the audit
        still_going = self._write("alpha", 2)   # this minute's progress
        # Aged LAST, after every write. Raw SQL against a table the ORM is
        # still holding records for is only reliable once it has nothing left
        # to say about them.
        self._age(delivered, 3)

        self.events.cron_prune_delivered_events()

        self.assertEqual(self._rows_of("alpha"), still_going)

    def test_the_retention_is_stated_in_whole_hours_and_is_short(self):
        self.assertEqual(event_model.EVENT_RETENTION_HOURS, 1)


class TheModelsThinkingIsNeverWritten(_EventsTestCase):
    """What the relay stores, and what it drops at the door."""

    def setUp(self):
        super().setUp()
        # THE RELAY COMMITS EVERY EVENT ON PURPOSE -- an event held back until
        # the end of the turn is not progress, it is a transcript delivered
        # late. In a test that commit releases the savepoint the case is
        # running inside, so the case that follows opens on a dead
        # transaction: four unrelated tests failed for something none of them
        # did. Held here instead, so the whole path runs exactly as it does
        # live and only the durability is postponed to the rollback.
        committing = patch.object(type(self.env.cr), "commit", lambda _cr: None)
        committing.start()
        self.addCleanup(committing.stop)

    def _relay(self, lines):
        session = self._session("relayed")
        stream = _Stream(lines)
        turns._relay(self.env, self.env.uid, session, stream)
        return [row.event for row in self._rows_of("relayed")]

    def test_reasoning_never_becomes_a_row(self):
        written = self._relay(
            _sse("reasoning", {"text": "Let me think about the ledger."})
            + _sse("text", {"text": "The balance is 400."})
            + _sse("done", {"response": "The balance is 400."})
        )
        self.assertNotIn("agent_message_reasoning", written)

    def test_the_answer_itself_is_still_written(self):
        written = self._relay(
            _sse("text", {"text": "The balance is 400."})
            + _sse("done", {"response": "The balance is 400."})
        )
        self.assertIn("agent_message_chunk", written)
        self.assertIn("agent_message_done", written)

    def test_the_milestones_the_customer_follows_are_still_written(self):
        written = self._relay(
            _sse("reasoning", {"text": "thinking"})
            + _sse("node_start", {"node": "read", "label": "Reading the general ledger"})
            + _sse("tool_start", {"tool": "send_message", "label": "Sending the report"})
            + _sse("todo", {"status": "active", "tasks": [{"title": "Read the ledger"}]})
            + _sse("done", {"response": "Sent."})
        )
        self.assertIn("agent_node_start", written)
        self.assertIn("agent_tool_start", written)
        self.assertIn("agent_todo_update", written)
        self.assertNotIn("agent_message_reasoning", written)

    def test_dropping_the_thinking_does_not_disturb_the_words_around_it(self):
        """Prose either side of dropped thinking must still arrive in order."""
        self._relay(
            _sse("text", {"text": "first "})
            + _sse("reasoning", {"text": "(thinking)"})
            + _sse("text", {"text": "second"})
            + _sse("done", {"response": "first second"})
        )
        chunks = self._rows_of("relayed").filtered(
            lambda row: row.event == "agent_message_chunk")
        spoken = "".join(json.loads(row.payload)["chunk"] for row in chunks)
        self.assertEqual(spoken, "first second")

    def test_a_managers_aside_is_stored_and_drawn_as_its_own_bubble(self):
        session = self._session("relayed")
        turns._relay(self.env, self.env.uid, session, _Stream(
            _sse("aside", {"text": "Added the VAT check as step 4."})
            + _sse("done", {"response": "Done."})
        ))
        written = [row.event for row in self._rows_of("relayed")]
        self.assertIn("agent_aside", written)
        stored = self.env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id), ("sender", "=", "ai")], order="id asc")
        self.assertEqual(stored[0].content, "Added the VAT check as step 4.")

    def test_a_noted_message_ends_the_turn_without_an_answer_row(self):
        session = self._session("relayed")
        turns._relay(self.env, self.env.uid, session, _Stream(_sse("noted", {"session_id": "relayed"})))
        written = [row.event for row in self._rows_of("relayed")]
        self.assertEqual(written, ["agent_message_noted"])
        stored = self.env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id), ("sender", "=", "ai")])
        self.assertFalse(stored, "a noted message stores no answer of its own")

    def test_the_relay_speaks_exactly_the_servers_vocabulary(self):
        """The events the Frappe relay handles, event for event; the ones
        nothing has emitted since the manager rewrite are not among them."""
        import inspect
        import re

        handled = set(re.findall(r'current_event == "([a-z_]+)"', inspect.getsource(turns._relay)))
        self.assertEqual(handled, {"text", "reasoning", "node_start", "todo", "tool_start",
                                   "aside", "noted", "done", "cancelled", "error"})
