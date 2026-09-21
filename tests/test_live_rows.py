# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""A statement the agent sends cannot reach a draft or cancelled row.

Runs standalone — no Odoo installation required, for the same reason
``test_query_guard.py`` does: ``razyyn_ai/services/live_rows.py`` has no
``odoo`` import, takes what a table's live rows are from a callback, and is
loaded here straight from its file path. The site-side half — reading the
model's own ``state`` selection — is ``_live_filter_for_table`` in
``agent_api_service.py`` and is exercised inside a real Odoo by
``razyyn_ai/tests``.

    cd tests && python3 -m unittest test_live_rows -v
"""

import importlib.util
import os
import sys
import unittest
from unittest import mock

_here = os.path.dirname(os.path.abspath(__file__))
_module_path = os.path.join(_here, "..", "razyyn_ai", "services", "live_rows.py")
_spec = importlib.util.spec_from_file_location("razyyn_live_rows_under_test", _module_path)
live_rows = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = live_rows
_spec.loader.exec_module(live_rows)


def _filter(table):
    """The doubled site: moves and their lines say posted, everything else
    is either a master or no model at all."""
    return {
        "account_move": "state = 'posted'",
        "account_move_line": "parent_state = 'posted'",
        "sale_order": "state = 'sale'",
    }.get(table)


def _rewrite(sql):
    return live_rows.exclude_dead_rows(sql, "postgres", _filter)


class ADraftOrCancelledRowIsUnreachable(unittest.TestCase):
    def test_the_ledger_is_read_through_its_posted_lines(self):
        sql, applied, enforced = _rewrite(
            "SELECT account_id, SUM(debit) AS d FROM account_move_line "
            "WHERE date >= '2026-01-01' GROUP BY account_id"
        )
        self.assertTrue(enforced)
        self.assertEqual(applied, {"account_move_line": "parent_state = 'posted'"})
        self.assertIn(
            '(SELECT * FROM "account_move_line" WHERE parent_state = \'posted\') AS "account_move_line"',
            sql,
        )
        self.assertIn("date >= '2026-01-01'", sql)
        self.assertIn("GROUP BY account_id", sql)

    def test_an_aliased_table_keeps_its_alias_and_a_master_is_left_alone(self):
        sql, applied, _ = _rewrite(
            "SELECT m.name, p.name FROM account_move m JOIN res_partner p ON p.id = m.partner_id"
        )
        self.assertIn("(SELECT * FROM \"account_move\" WHERE state = 'posted') AS \"m\"", sql)
        self.assertIn("m.partner_id", sql)
        self.assertNotIn("res_partner\" WHERE", sql)
        self.assertEqual(list(applied), ["account_move"])

    def test_every_reference_is_filtered_not_only_the_first(self):
        sql, _, _ = _rewrite(
            "SELECT name FROM account_move WHERE amount_total > 1000 "
            "UNION ALL "
            "SELECT name FROM account_move WHERE partner_id IN "
            "(SELECT partner_id FROM account_move WHERE amount_residual > 0)"
        )
        self.assertEqual(sql.count("WHERE state = 'posted'"), 3)

    def test_a_cte_is_read_through_live_rows_and_its_own_name_is_not_a_table(self):
        sql, applied, enforced = _rewrite(
            "WITH sold AS (SELECT partner_id, SUM(amount_total) AS t FROM sale_order GROUP BY partner_id) "
            "SELECT * FROM sold WHERE t > 0"
        )
        self.assertTrue(enforced)
        self.assertIn("WHERE state = 'sale'", sql)
        self.assertEqual(list(applied), ["sale_order"])
        self.assertNotIn('(SELECT * FROM "sold"', sql)

    def test_this_databases_own_syntax_survives_the_round_trip(self):
        """The column rewriter has already put `->>` and `::` into the
        statement by the time it gets here; both must come out intact."""
        sql, _, _ = _rewrite(
            "SELECT a.name->>'en_US' AS name, m.date::date AS d "
            "FROM account_move m JOIN account_account a ON a.id = m.id "
            "WHERE (m.create_date AT TIME ZONE 'UTC') AT TIME ZONE 'Africa/Cairo' > m.date"
        )
        self.assertIn("->> 'en_US'", sql)
        # The cast is written back in its long form; Postgres reads both.
        self.assertTrue("::DATE" in sql.upper() or "CAST(M.DATE AS DATE)" in sql.upper(), sql)
        self.assertIn("AT TIME ZONE 'Africa/Cairo'", sql)
        self.assertIn("WHERE state = 'posted'", sql)

    def test_a_table_whose_every_row_is_real_is_left_exactly_as_written(self):
        original = "SELECT id, name FROM res_partner WHERE active"
        sql, applied, enforced = _rewrite(original)
        self.assertTrue(enforced)
        self.assertEqual(applied, {})
        self.assertEqual(sql, original)

    def test_information_schema_is_untouched(self):
        original = "SELECT column_name FROM information_schema.columns WHERE table_name = 'account_move'"
        sql, applied, _ = _rewrite(original)
        self.assertEqual(applied, {})
        self.assertEqual(sql, original)

    def test_a_statement_the_parser_cannot_read_runs_as_written_and_says_so(self):
        original = "SELECT )( FROM account_move"
        sql, applied, enforced = _rewrite(original)
        self.assertEqual(sql, original)
        self.assertEqual(applied, {})
        self.assertFalse(enforced)

    def test_a_site_without_the_parser_runs_as_written_and_says_so(self):
        with mock.patch.object(live_rows, "sqlglot", None):
            sql, _applied, enforced = _rewrite("SELECT 1 FROM account_move")
        self.assertEqual(sql, "SELECT 1 FROM account_move")
        self.assertFalse(enforced)

    def test_a_callback_that_raises_is_no_filter_not_a_failed_read(self):
        def broken(_table):
            raise RuntimeError("registry unavailable")

        sql, applied, enforced = live_rows.exclude_dead_rows("SELECT 1 FROM account_move", "postgres", broken)
        self.assertTrue(enforced)
        self.assertEqual(applied, {})
        self.assertEqual(sql, "SELECT 1 FROM account_move")

    def test_the_flag_is_read_however_the_transport_spelled_it(self):
        for spelled in (True, "true", "1", "yes", "True"):
            self.assertTrue(live_rows.read_flag(spelled), spelled)
        for spelled in (False, None, "", "false", "0", "no"):
            self.assertFalse(live_rows.read_flag(spelled), spelled)


if __name__ == "__main__":
    unittest.main()
