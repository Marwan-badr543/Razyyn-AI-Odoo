# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Runs standalone — no Odoo installation required.

WHAT THIS PROTECTS
    On Odoo the chat window is fed from a table: every piece of the answer the
    agent writes used to be a row of its own, committed on its own, and found
    again by every open chat window's next look. A long report is thousands of
    pieces, and two conversations being answered at once was enough to slow the
    whole site to the point where the browser offered to close the page.

    ``HeldWords`` gathers those pieces for a quarter of a second so they travel
    as one row. That is only safe while two promises hold, and these are them:

      1. NOTHING IS LOST OR REORDERED. Joining what comes out must give back
         exactly what went in, in the order it went in.
      2. A MILESTONE NEVER OVERTAKES THE WORDS BEFORE IT. A step starting, the
         checklist changing, the answer finishing — each of those has to arrive
         after the prose that preceded it, or the chat window draws the story in
         the wrong order.

    Both are invisible when they break: the customer simply reads a garbled
    transcript. Hence a test rather than a comment.
"""

import importlib.util
import os
import sys
import unittest

_here = os.path.dirname(os.path.abspath(__file__))
_module_path = os.path.join(_here, "..", "razyyn_ai", "services", "held_words.py")
_spec = importlib.util.spec_from_file_location("razyyn_held_words_under_test", _module_path)
held_words = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = held_words
_spec.loader.exec_module(held_words)

HeldWords = held_words.HeldWords
CHUNK_HOLD_CHARS = held_words.CHUNK_HOLD_CHARS


class Recorder:
    """A stand-in for the event table: remembers what was actually sent."""

    def __init__(self):
        self.sent = []

    def __call__(self, event, text):
        self.sent.append((event, text))

    def joined(self, event):
        return "".join(text for name, text in self.sent if name == event)


ANSWER = "agent_message_chunk"
NOTES = "agent_message_reasoning"


class TestNothingIsLost(unittest.TestCase):

    def test_every_letter_arrives_in_order(self):
        sent = Recorder()
        held = HeldWords(sent)
        words = ["The ", "trial ", "balance ", "does ", "not ", "agree."]
        for word in words:
            held.hold(ANSWER, word)
        held.flush()
        self.assertEqual(sent.joined(ANSWER), "".join(words))

    def test_a_quarter_second_of_writing_travels_as_one_row(self):
        """The whole point: fewer rows, same words."""
        sent = Recorder()
        held = HeldWords(sent)
        for _ in range(50):
            held.hold(ANSWER, "x")
        held.flush()
        self.assertEqual(sent.joined(ANSWER), "x" * 50)
        self.assertLess(len(sent.sent), 50)

    def test_a_very_long_stretch_is_not_held_indefinitely(self):
        """A burst that arrives faster than the clock still goes out."""
        sent = Recorder()
        held = HeldWords(sent)
        held.hold(ANSWER, "y" * (CHUNK_HOLD_CHARS + 1))
        self.assertEqual(sent.joined(ANSWER), "y" * (CHUNK_HOLD_CHARS + 1))

    def test_an_empty_piece_sends_nothing(self):
        sent = Recorder()
        held = HeldWords(sent)
        held.hold(ANSWER, "")
        held.flush()
        self.assertEqual(sent.sent, [])

    def test_flushing_twice_does_not_repeat_the_words(self):
        sent = Recorder()
        held = HeldWords(sent)
        held.hold(ANSWER, "once")
        held.flush()
        held.flush()
        self.assertEqual(sent.joined(ANSWER), "once")


class TestTheTwoKindsOfProseStaySeparate(unittest.TestCase):

    def test_the_answer_and_the_working_notes_never_merge(self):
        """They are drawn in two different places and must stay two streams."""
        sent = Recorder()
        held = HeldWords(sent)
        held.hold(ANSWER, "the answer")
        held.hold(NOTES, "the notes")
        held.flush()
        self.assertEqual(sent.joined(ANSWER), "the answer")
        self.assertEqual(sent.joined(NOTES), "the notes")


class TestAMilestoneNeverOvertakes(unittest.TestCase):

    def test_a_milestone_sees_the_words_that_came_before_it(self):
        """This is what _relay does: flush, then publish the milestone."""
        sent = Recorder()
        held = HeldWords(sent)
        held.hold(ANSWER, "half a sentence")

        # A step starts. _relay flushes before publishing anything that is not
        # prose; standing in for that publish with a marker of our own.
        held.flush()
        sent("agent_node_start", "")

        held.hold(ANSWER, " and the rest")
        held.flush()

        self.assertEqual(
            [event for event, _ in sent.sent],
            [ANSWER, "agent_node_start", ANSWER],
        )


if __name__ == "__main__":
    unittest.main()
