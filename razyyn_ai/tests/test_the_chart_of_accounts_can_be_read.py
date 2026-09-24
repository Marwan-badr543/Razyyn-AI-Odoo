# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Finding an account, on a version that moved where an account's code lives.

WHAT A LIVE SESSION SPENT ITSELF ON
    Asked to record a journal entry, the desk went looking for the accounts to
    post it to, and never found them. Fourteen round trips, every one refused:

        FROM tabAccount                  -> relation "tabaccount" does not exist
        SELECT code FROM account_account -> column "code" does not exist
        SELECT company_id ...            -> column "company_id" does not exist

    None of the three is the desk being stupid. `tabAccount` is what the same
    document is called in the other ERP this agent writes to. `code` is what
    Odoo's own Chart of Accounts screen, the field's label and every accountant
    alive calls it. `company_id` is on nearly every other model in Odoo.

    Odoo 18 is simply different underneath: `code` is computed, its value kept
    per company in `code_store` (JSON keyed by the ROOT company's id), and an
    account belongs to companies through `company_ids`, a many-to-many. None of
    that is visible from outside, and the refusals did not say it — "Did you
    mean: code_store, note?" was the whole of the help, and `code_store` is a
    trap: selected bare it returns a JSON object, so taking the suggestion
    yields something that is not a code and teaches nothing.

WHAT THIS MODULE DOES ABOUT IT NOW
    Corrects what it can and explains the rest, in that order. A Frappe table
    name resolves to the model it names; a field that is not a column is
    rewritten to the column that holds it; and whatever is still refused is
    refused with the expression that would have worked.

ONE FILE, BOTH VERSIONS. Nothing below names 17 or 18. Each case asks the live
schema which shape this database is and checks the right answer for it, which
is the only way a test can be true of both.
"""

from odoo.tests.common import TransactionCase

from ..services import agent_api_service as reads


class TheChartOfAccountsCanBeRead(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].search([], limit=1)
        self.columns = reads._actual_columns(self.env.cr, "account_account")
        #: True on the versions that moved the code off the table.
        self.code_moved = "code" not in self.columns

    def _run(self, sql):
        return reads.validate_and_execute_query(
            self.env, sql, company_id=self.company.id)

    def _rows(self, result):
        return result.get("rows") or result.get("data") or []

    # ── the query anyone would write ─────────────────────────────────────────

    def test_an_account_s_code_can_be_selected_by_the_name_it_is_called(self):
        """THE ONE THAT MATTERS. `code` is the field's name on every screen;
        whether it is a column is this version's business, not the caller's."""
        rows = self._rows(self._run(
            "SELECT id, code, name FROM account_account ORDER BY id LIMIT 5"))
        self.assertTrue(rows, "this database has no accounts to read")
        codes = [value for row in rows for key, value in row.items()
                 if key.startswith("code")]
        self.assertTrue(all(codes), f"every account has a code: {rows}")

    def test_and_can_be_searched_by_it(self):
        rows = self._rows(self._run(
            "SELECT id, code, name FROM account_account ORDER BY id LIMIT 1"))
        wanted = next(value for key, value in rows[0].items()
                      if key.startswith("code"))
        found = self._rows(self._run(
            f"SELECT id FROM account_account WHERE code = '{wanted}'"))
        # `in`, not `==`. A code is unique within a company, and a database
        # with two companies has two charts — on the version that still keeps
        # the code in a plain column, both rows come back, correctly. What is
        # being checked here is that searching BY THE NAME THE FIELD IS CALLED
        # finds the account at all.
        self.assertIn(rows[0]["id"], [row["id"] for row in found])

    def test_the_other_erp_s_name_for_the_table_reaches_this_one_s(self):
        """`tabAccount` is Frappe's spelling. The name identifies the document
        perfectly well, so it is read rather than refused."""
        self.assertIn(
            "FROM account_account",
            reads.rewrite_model_names(
                self.env, "SELECT id FROM tabAccount"),
        )

    def test_a_table_this_system_really_has_is_never_reinterpreted(self):
        """The rewrite fires only when the literal name is not a table here AND
        the name without the prefix resolves. Neither half may be dropped."""
        for sql in ("SELECT id FROM account_account",
                    "SELECT id FROM tabNothingLikeThis"):
            self.assertEqual(reads.rewrite_model_names(self.env, sql), sql)

    # ── and when it still cannot be corrected, it is explained ───────────────

    def test_a_field_that_is_not_a_column_is_answered_with_the_expression(self):
        if not self.code_moved:
            self.skipTest("this version stores the code as an ordinary column")
        said = reads._where_this_field_really_is(
            self.env, "account.account", "code")
        self.assertIn("code_store->>", said)
        self.assertIn("ROOT company", said)

    def test_a_field_this_version_renamed_is_answered_with_the_new_one(self):
        if "company_id" in self.columns:
            self.skipTest("this version still has company_id on an account")
        said = reads._where_this_field_really_is(
            self.env, "account.account", "company_id")
        self.assertIn("company_ids", said)
        self.assertIn("account_account_res_company_rel", said)

    def test_the_correction_survives_the_client_s_300_character_cap(self):
        """THE CAP IS PART OF THE CONTRACT. The agent's HTTP client keeps the
        first 300 characters of an upstream error and drops the rest, so a
        correction that arrives after that budget is a correction nobody reads
        — which is how the first attempt at this fix still left the desk
        looping. The expression has to be inside the window."""
        if not self.code_moved:
            self.skipTest("this version stores the code as an ordinary column")
        with self.assertRaises(reads.QueryExecutionError) as caught:
            reads.validate_and_execute_query(
                self.env,
                "SELECT id, code FROM account_account WHERE code = '1'",
                company_id=None,
            )
        self.assertIn("code_store->>", str(caught.exception)[:300])

    def test_the_schema_reply_and_a_refusal_say_the_same_thing(self):
        """They used to disagree, and the one the desk actually met — the
        error — was the one that had it wrong."""
        if not self.code_moved:
            self.skipTest("this version stores the code as an ordinary column")
        said = reads._where_this_field_really_is(
            self.env, "account.account", "code")
        summary = reads.build_schema_summary(self.env, "account.account")
        self.assertIn(said, summary["related_tables"])

    # ── the failure that would be worse than the one being fixed ─────────────

    def test_nothing_is_rewritten_into_a_silently_empty_answer(self):
        """A per-company column compared to a text literal is a jsonb-to-text
        comparison: PostgreSQL returns NO ERROR AND NO ROWS. Told nothing about
        which company, this must refuse loudly rather than rename into that."""
        if not self.code_moved:
            self.skipTest("this version stores the code as an ordinary column")
        self.assertEqual(
            reads.speak_this_database(
                self.env,
                "SELECT id FROM account_account WHERE code = '101000'",
                company_id=None,
            ),
            "SELECT id FROM account_account WHERE code = '101000'",
        )

    def test_a_connection_always_carries_the_company_that_makes_it_work(self):
        """The rename above is gated on knowing a company, so the gate is only
        safe if the live path always has one. It is required on the record."""
        self.assertTrue(
            self.env["razyyn.agent.settings"]._fields["company_id"].required)
