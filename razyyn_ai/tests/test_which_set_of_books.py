# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""A name means one thing per company, and the connection says which company.

WHAT A SECOND COMPANY DOES TO EVERY NAME THE AGENT SENDS
    A business with two companies has two journals called "Miscellaneous
    Operations", two accounts called "Bank", two of nearly everything —
    correctly, because they are two sets of books. So "record this in
    Miscellaneous Operations" matches twice, and the write side refused the
    document as ambiguous:

        "Miscellaneous Operations" matches more than one of your
        account.journal records ("Miscellaneous Operations",
        "Miscellaneous Operations"). Tell me which one and I will use it.

    The same journal named twice, and no way for anybody to act on it.

THE ANSWER WAS ALREADY ON THE CONNECTION
    A Razyyn connection is issued against one company and the field is
    REQUIRED on it. The read side has resolved per-company values through it
    since those columns arrived; the write side never received it, which is
    the whole of this bug. The payload still wins wherever it speaks — this is
    only the answer to a question it did not answer.
"""

from odoo.tests.common import TransactionCase

from ..services import agent_write_service as writes
from ..services.write_guard import WriteRejectedError


class WhichSetOfBooks(TransactionCase):

    def setUp(self):
        super().setUp()
        # THE MASTER SWITCH IS OFF ON A DATABASE NOBODY HAS SET UP YET.
        # `enabled` defaults to False, and `preflight_document` answers a
        # refused policy with ONE finding and returns — so without this, every
        # case below reads `['policy']` instead of the finding it is about, and
        # the gap this file exists to catch is invisible. `test_the_create_desk`
        # opens the same switch for the same reason.
        self.env["razyyn.agent.write.policy"].sudo().get_policy_singleton().write({
            "enabled": True, "dry_run_only": False,
            "restrict_to_listed_models": False,
            "max_documents_per_run": 0, "max_total_amount_per_run": 0.0,
        })
        self.mine = self.env["res.company"].search([], limit=1)
        self.theirs = self.env["res.company"].create(
            {"name": "Razyyn Second Books"})
        # The same name in both sets of books, which is the whole situation.
        shared = "Razyyn Shared Journal Name"
        self.ours = self.env["account.journal"].create({
            "name": shared, "code": "RZJ1", "type": "general",
            "company_id": self.mine.id,
        })
        self.other = self.env["account.journal"].create({
            "name": shared, "code": "RZJ2", "type": "general",
            "company_id": self.theirs.id,
        })
        self.shared_name = shared

    def _resolve(self, company=None):
        return writes._odoo_values(
            self.env, "account.move",
            {"move_type": "entry", "journal_id": self.shared_name},
            company=company,
        )["journal_id"]

    def test_without_the_connection_s_company_the_name_is_ambiguous(self):
        """Stated plainly, because it is what the fix is measured against."""
        with self.assertRaises(WriteRejectedError) as caught:
            self._resolve()
        self.assertEqual(caught.exception.code, "LINK_AMBIGUOUS")

    def test_the_connection_s_company_settles_it(self):
        self.assertEqual(self._resolve(company=self.mine.id), self.ours.id)
        self.assertEqual(self._resolve(company=self.theirs.id), self.other.id)

    def test_what_the_payload_says_still_wins(self):
        """The default answers a question the payload did not; it never
        overrides one it did. A customer who names the company means it."""
        resolved = writes._odoo_values(
            self.env, "account.move",
            {"move_type": "entry",
             "company_id": self.theirs.name,
             "journal_id": self.shared_name},
            company=self.mine.id,
        )
        self.assertEqual(resolved["journal_id"], self.other.id)

    def test_a_name_only_one_company_has_is_unaffected(self):
        only_here = self.env["account.journal"].create({
            "name": "Razyyn Only In One Company", "code": "RZJ3",
            "type": "general", "company_id": self.mine.id,
        })
        for company in (None, self.mine.id, self.theirs.id):
            self.assertEqual(
                writes._odoo_values(
                    self.env, "account.move",
                    {"move_type": "entry",
                     "journal_id": "Razyyn Only In One Company"},
                    company=company,
                )["journal_id"],
                only_here.id,
            )

    def test_preflight_and_the_write_are_told_the_same_company(self):
        """They are two calls and they must not disagree: a preflight that
        passes followed by a write that refuses is the worst of both."""
        payload = {"doctype": "account.move", "move_type": "entry",
                   "journal_id": self.shared_name,
                   "line_ids": [{"name": "x", "debit": 1.0},
                                {"name": "x", "credit": 1.0}]}
        ambiguous = writes.preflight_document(self.env, payload)["findings"]
        self.assertTrue(any("more than one" in f["human_message"]
                            for f in ambiguous))
        settled = writes.preflight_document(
            self.env, payload, default_company=self.mine.id)["findings"]
        self.assertFalse(any("more than one" in f["human_message"]
                             for f in settled),
                         f"the company was given and should have settled it: "
                         f"{settled}")
