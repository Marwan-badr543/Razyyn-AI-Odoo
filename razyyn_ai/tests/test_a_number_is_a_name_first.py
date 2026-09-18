# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""An account code that looks like a number must not be read as a database id.

WHAT WENT WRONG
    `_link_target` began with `if text.isdigit(): return int(text)`, before any
    lookup at all. Most of the world numbers its chart of accounts, so
    `account_id: "400050"` -- the code an accountant reads off their own trial
    balance, and exactly what this protocol says a reference carries -- went to
    the database as primary key 400050.

    On the site it was found, that key did not exist and PostgreSQL refused the
    line. That was the lucky outcome. On a database where some unrelated
    account happened to hold that id, the entry would have posted to it,
    balanced perfectly, and been found at year end.

    A number is a name first and an id only when it names nothing.
"""

from odoo.tests.common import TransactionCase

from ..services import agent_write_service as writes
from ..services.write_guard import AgentWriteError


class ANumberIsANameFirst(TransactionCase):

    def setUp(self):
        super().setUp()
        self.account = self.env["account.account"].search([], limit=1)

    def test_a_numeric_code_finds_the_account_with_that_code(self):
        # A code held by two companies is genuinely ambiguous and is refused by
        # design -- that is its own test below. This one is about the ordinary
        # case, so it needs a code only one account carries.
        counted = {}
        for candidate in self.env["account.account"].search([]):
            if (candidate.code or "").isdigit():
                counted.setdefault(candidate.code, []).append(candidate)
        unique = [rows[0] for rows in counted.values() if len(rows) == 1]
        if not unique:
            self.skipTest("Every numeric account code here is held twice.")
        account = unique[0]

        found = writes._link_target(
            self.env, "account.account", "account_id", account.code)

        self.assertEqual(
            found, account.id,
            f"code {account.code!r} must find the account that carries it, not "
            f"the record whose primary key happens to be {account.code}.",
        )

    def test_a_number_naming_nothing_falls_back_to_the_id(self):
        """Callers that genuinely send an id are still understood.

        The number has to be one that names NOTHING, which is the only case
        where an id is what it can mean. A small id like "1" is a substring of
        half a chart of accounts, and being refused for that is the guard
        working, not failing -- see the ambiguity case below.
        """
        accounts = self.env["account.account"].search([])
        if not accounts:
            self.skipTest("This database has no accounts.")
        unnamed = next(
            (a for a in accounts
             if not a.sudo().name_search(name=str(a.id), operator="ilike", limit=1)),
            None,
        )
        if unnamed is None:
            self.skipTest("Every account id here also reads as part of a name.")

        found = writes._link_target(
            self.env, "account.account", "account_id", str(unnamed.id))
        self.assertEqual(found, unnamed.id)

    def test_a_number_that_is_neither_is_refused_not_written(self):
        with self.assertRaises(AgentWriteError) as refused:
            writes._link_target(
                self.env, "account.account", "account_id", "987654321")
        self.assertIn("987654321", str(refused.exception))


    def test_a_code_two_companies_share_is_refused_and_both_are_named(self):
        """The refusal is the right answer, and it has to be actionable.

        On a multi-company database the same code is issued in each company's
        chart. Quietly taking the first is how an entry lands in the wrong
        company's books and is found at year end, so the refusal names the
        candidates and asks.
        """
        counted = {}
        for candidate in self.env["account.account"].search([]):
            if candidate.code:
                counted.setdefault(candidate.code, []).append(candidate)
        shared = [code for code, rows in counted.items() if len(rows) > 1]
        if not shared:
            self.skipTest("This database has one company's chart of accounts.")

        with self.assertRaises(AgentWriteError) as refused:
            writes._link_target(
                self.env, "account.account", "account_id", shared[0])
        message = str(refused.exception)
        self.assertIn(shared[0], message)
        self.assertIn("more than one", message)
