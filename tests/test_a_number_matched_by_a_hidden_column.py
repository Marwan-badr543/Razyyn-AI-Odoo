# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""An invoice was posted to the wrong customer, and the card said otherwise.

WHAT HAPPENED, LIVE (2026-09-22)
    The agent resolved the customer correctly. Its write log for the run reads

        create account.move  payload {"partner_id": "15", ...}

    and 15 is Azure Interior's id -- an id this very module had issued to the
    agent, because `search_documents` answers with `docname = str(record.id)`.

    `_link_target` then refused to read it back as an id. It only ever
    short-circuits a real `int`, so the STRING "15" went to `name_search`,
    which on `res.partner` searches `complete_name`, `email`, `ref`, `vat` and
    `company_registry`. Exactly one record on that database contains "15"
    anywhere in those columns: Soham Palmer, whose demo email is
    `soham.palmer15@example.com`. One match, so `pick_link_match`'s rule that
    a lone candidate is taken whatever it scores wrote Soham Palmer into the
    invoice.

    The approval card the customer had already read said "partner_id: Azure
    Interior", and it was telling the truth -- the substitution happened after
    they approved it. The invoice was posted under a real number, to the wrong
    company. It took six more approval rounds to unpick, because a draft
    `account.move` displays as the bare string "Draft Invoice", so no card in
    any of those rounds could show which party it was actually about.

WHAT MUST NOT BREAK WHILE FIXING IT
    A number is still a NAME first. Most of the world numbers its chart of
    accounts, and `account_id: "101000"` is the code an accountant reads off
    their own trial balance. The difference is where the number matched: a
    code lives in the displayed name Odoo renders ("101000 Current Assets"),
    and an email address does not. That is the whole rule.
"""

import importlib.util
import os
import unittest

# NO ODOO MOCK HERE, deliberately. `link_match` imports nothing from Odoo --
# that is why it is its own module -- and the MagicMock the other standalone
# tests install for `odoo` makes pytest read a mocked `pytest_plugins` off it
# and refuse to collect. A rule this consequential must be runnable with
# nothing but Python.
_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "link_match",
    os.path.join(_here, "..", "razyyn_ai", "services", "link_match.py"),
)
link_match = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(link_match)

pick_link_match = link_match.pick_link_match
named_by = link_match.named_by


class ANumberMatchedByAHiddenColumn(unittest.TestCase):
    """The live failure, in the shape `name_search` actually returned it."""

    #: Exactly what `res.partner.name_search("15", operator="ilike", limit=8)`
    #: answered on the database where the wrong invoice was posted. Verified in
    #: an `odoo-bin shell` against that site, not invented for this test.
    SOHAM = [(24, "Gemini Furniture, Soham Palmer")]

    def test_the_lone_hidden_match_is_not_taken(self):
        self.assertEqual(
            pick_link_match("15", self.SOHAM), [],
            "A partner whose EMAIL contains the digits is not the record "
            "somebody meant by those digits. Taking it posted an invoice to "
            "the wrong company.",
        )

    def test_and_so_the_number_is_left_to_be_read_as_an_id(self):
        """The empty answer is the point: `_link_target` tries the id next.

        `pick_link_match` returning nothing is what lets the `isdigit()` branch
        below it browse record 15 and find Azure Interior. A single wrong
        match short-circuits that branch entirely, which is why filtering has
        to happen HERE and not after.
        """
        self.assertFalse(pick_link_match("15", self.SOHAM))

    def test_an_account_code_still_finds_its_account(self):
        """The case the previous behaviour existed to protect. Unchanged.

        Odoo renders an account as "<code> <name>", so the code a person reads
        off their trial balance is inside the label and survives the filter.
        This pair is real: `account.account.name_search("101000")` on the same
        database.
        """
        self.assertEqual(
            pick_link_match("101000", [(1, "101000 Current Assets")]), 1,
        )

    def test_a_code_shared_by_two_companies_is_still_ambiguous(self):
        """Both carry the code in their label, so both survive and both are
        offered. Quietly taking one is how an entry lands in the wrong set of
        books."""
        shared = [(1, "101000 Current Assets"), (2, "101000 Current Assets")]
        self.assertEqual(pick_link_match("101000", shared), shared)

    def test_a_record_actually_named_by_the_digits_wins(self):
        """A number is a name first. A partner literally called "15" carries
        the digits in the name a person reads, so it is a real match."""
        self.assertEqual(
            pick_link_match("15", [(99, "15"), (24, "Gemini, Soham Palmer")]),
            99,
        )

    def test_words_are_not_filtered_at_all(self):
        """The filter applies to digits and to nothing else.

        A partner found by their reference or their email from a WORD search
        is the breadth of `name_search` working as intended -- it is only a
        trap for short numbers.
        """
        matches = [(15, "Azure Interior"), (27, "Azure Interior, Brandon Freeman")]
        self.assertEqual(named_by("Azure Interior", matches), matches)
        self.assertEqual(pick_link_match("Azure Interior", matches), 15)

    def test_a_number_inside_a_longer_name_is_kept(self):
        """"Draft Invoice (* 230)" is how this system displays an unposted
        entry, and 230 is in the name the customer reads."""
        self.assertEqual(
            pick_link_match("230", [(230, "Draft Invoice (* 230)")]), 230,
        )

    def test_nothing_at_all_is_still_nothing(self):
        self.assertEqual(pick_link_match("15", []), [])


if __name__ == "__main__":
    unittest.main()
