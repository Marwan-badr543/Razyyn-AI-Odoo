# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""A change to a document's lines must leave the lines it was given, and no more.

WHAT WENT WRONG, LIVE, ON A REAL LEDGER
    A customer asked for the two lines of a 300.00 draft entry to read 450.00.
    The payload carried exactly that: two lines, 450.00 each way. Odoo's ORM
    reads a bare list of rows on a one-to-many as ``(0, 0, row)`` -- "ADD this
    row" -- so it appended them. The entry ended with FOUR lines and a value of
    750.00 on each side.

    Nothing complained. It still balanced, the write log said success, and the
    agent told the customer the entry "now records a 450.00 expense" about a
    document holding 750.00. An accounting agent that inflates an entry while
    reporting the figure the customer asked for is the worst failure this
    module has: their books are wrong and their receipt says otherwise.

    A table sent with a change is the whole of what that table should say
    afterwards. There is no other reading: the payload carries no row
    identities to match existing rows against.
"""

from odoo.tests.common import TransactionCase

from ..services import agent_write_service as writes


class ChangeReplacesTheTable(TransactionCase):

    def setUp(self):
        super().setUp()
        policy = self.env["razyyn.agent.write.policy"].sudo().get_policy_singleton()
        policy.write({
            "enabled": True, "dry_run_only": False,
            "restrict_to_listed_models": False,
            "max_documents_per_run": 0, "max_total_amount_per_run": 0.0,
        })
        self.journal = self.env["account.journal"].search(
            [("type", "=", "general")], limit=1)
        accounts = self.env["account.account"]
        if self.journal:
            domain = ([("company_ids", "in", self.journal.company_id.ids)]
                      if "company_ids" in accounts._fields
                      else [("company_id", "=", self.journal.company_id.id)])
            accounts = accounts.search(domain, limit=2)
        self.accounts = accounts

    def _entry(self, amount):
        """One balanced draft entry, written the way the agent writes one."""
        return {
            "move_type": "entry",
            "journal_id": self.journal.id,
            "company_id": self.journal.company_id.id,
            "ref": "RAZYYN-REPLACE-TEST",
            "line_ids": [
                {"account_id": self.accounts[0].id, "debit": amount, "credit": 0.0},
                {"account_id": self.accounts[1].id, "debit": 0.0, "credit": amount},
            ],
        }

    def test_changing_the_lines_leaves_only_the_lines_given(self):
        if len(self.accounts) < 2 or not self.journal:
            self.skipTest("This database has no general journal with two accounts.")

        move = self.env["account.move"].sudo().create(
            writes._odoo_values(self.env, "account.move", self._entry(300.0))
        )
        self.assertEqual(len(move.line_ids), 2)
        self.assertEqual(sum(move.line_ids.mapped("debit")), 300.0)

        move.write(writes._odoo_values(
            self.env, "account.move",
            {"line_ids": self._entry(450.0)["line_ids"]},
            replacing=True,
        ))

        self.assertEqual(
            len(move.line_ids), 2,
            "A change that appends leaves the old lines behind: four lines, and "
            "an entry worth half as much again as the customer asked for.",
        )
        self.assertEqual(sum(move.line_ids.mapped("debit")), 450.0)
        self.assertEqual(sum(move.line_ids.mapped("credit")), 450.0)

    def test_a_creation_still_adds_its_lines(self):
        """The same translation, on the action where adding IS the meaning."""
        if len(self.accounts) < 2 or not self.journal:
            self.skipTest("This database has no general journal with two accounts.")

        values = writes._odoo_values(self.env, "account.move", self._entry(120.0))
        self.assertNotIn(
            5, [command[0] for command in values["line_ids"]],
            "A creation has nothing to clear, and clearing on the way in would "
            "be a command sent for no reason.",
        )
        move = self.env["account.move"].sudo().create(values)
        self.assertEqual(len(move.line_ids), 2)

    def test_a_change_that_does_not_mention_the_lines_leaves_them_alone(self):
        """Replacing is per FIELD, never per write.

        A customer correcting only a reference must not lose the entry's lines
        because the translation decided a change means "start again".
        """
        if len(self.accounts) < 2 or not self.journal:
            self.skipTest("This database has no general journal with two accounts.")

        move = self.env["account.move"].sudo().create(
            writes._odoo_values(self.env, "account.move", self._entry(300.0))
        )
        move.write(writes._odoo_values(
            self.env, "account.move", {"ref": "RAZYYN-REPLACE-TEST-2"},
            replacing=True,
        ))
        self.assertEqual(len(move.line_ids), 2)
        self.assertEqual(sum(move.line_ids.mapped("debit")), 300.0)
        self.assertEqual(move.ref, "RAZYYN-REPLACE-TEST-2")
