# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""The queries from a customer's log, and what has to happen to them.

Every SQL string in the first class below is one the agent really sent, copied
out of a session where it spent the whole conversation oscillating between two
errors instead of answering: `ILIKE` on a jsonb column, corrected to `->>` on a
varchar one, corrected back.

    cd tests && python3 -m unittest test_sql_dialect -v
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

_MODULE = pathlib.Path(__file__).resolve().parent.parent / "razyyn_ai"
_spec = importlib.util.spec_from_file_location(
    "razyyn_sql_dialect_under_test", _MODULE / "services" / "sql_dialect.py"
)
dialect = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dialect
_spec.loader.exec_module(dialect)


#: The real thing, read out of a stock Odoo 17 with information_schema.
#: `name` is jsonb on three of these and varchar on five, which is the whole
#: reason the agent could not get it right.
SCHEMA = {
    "account_account": {"id": "integer", "name": "jsonb", "code": "character varying",
                        "account_type": "character varying", "company_id": "integer"},
    "account_tax": {"id": "integer", "name": "jsonb", "type_tax_use": "character varying",
                    "company_id": "integer"},
    "product_template": {"id": "integer", "name": "jsonb", "list_price": "numeric",
                         "categ_id": "integer", "uom_id": "integer", "type": "character varying"},
    "product_category": {"id": "integer", "name": "character varying",
                         "complete_name": "character varying", "parent_id": "integer"},
    "product_product": {"id": "integer", "default_code": "character varying",
                        "product_tmpl_id": "integer"},
    "res_partner": {"id": "integer", "name": "character varying", "is_company": "boolean",
                    "customer_rank": "integer", "company_id": "integer", "active": "boolean"},
    "res_currency": {"id": "integer", "name": "character varying", "symbol": "character varying"},
    "res_company": {"id": "integer", "name": "character varying", "currency_id": "integer"},
    "ir_sequence": {"id": "integer", "name": "character varying", "code": "character varying"},
    # Odoo 18: the account code is per company, kept as JSON keyed by company id.
    "account_account_v18": {"id": "integer", "name": "jsonb", "code_store": "jsonb_company",
                            "account_type": "character varying"},
}


def run(sql, languages=("en_US",), company_id=None):
    return dialect.rewrite(sql, lambda table: SCHEMA.get(table, {}), languages,
                           company_id=company_id)


class TheQueriesThatFailed(unittest.TestCase):
    """Each of these came back as an error and the customer got nothing."""

    def test_ilike_on_a_translated_column(self):
        # "operator does not exist: jsonb ~~* unknown"
        out = run("SELECT id, list_price FROM product_template WHERE name ILIKE '%OPPO%'")
        self.assertIn("name->>'en_US' ILIKE '%OPPO%'", out)

    def test_ilike_on_an_account_name(self):
        out = run("SELECT id, code FROM account_account WHERE name ILIKE '%receivable%'")
        self.assertIn("name->>'en_US' ILIKE", out)

    def test_json_read_on_a_column_that_is_plain_text(self):
        # "operator does not exist: character varying ->> unknown"
        out = run("SELECT id, name->>'en_US' AS cat_name FROM product_category")
        self.assertNotIn("->>", out)
        self.assertIn("name AS cat_name", out)

    def test_the_correction_the_agent_made_to_its_own_correction(self):
        out = run("SELECT id, name->>'en_US' AS name FROM product_category ORDER BY name")
        self.assertNotIn("->>", out)

    def test_a_join_with_one_of_each(self):
        # product_template.name is jsonb, res_partner.name is varchar, in ONE
        # query. No single spelling works; qualifying them is what makes it
        # decidable, and this is the case a prompt can never fix.
        out = run(
            "SELECT pt.name, rp.name FROM product_template pt "
            "JOIN res_partner rp ON rp.id = pt.id WHERE pt.name ILIKE '%x%' "
            "AND rp.name ILIKE '%y%'"
        )
        self.assertIn("pt.name->>'en_US' ILIKE '%x%'", out)
        self.assertIn("AND rp.name ILIKE '%y%'", out)

    def test_a_site_with_its_own_language_falls_back_to_the_source_term(self):
        out = run("SELECT id FROM account_account WHERE name ILIKE '%bank%'", ("ar_001", "en_US"))
        self.assertIn("COALESCE(name->>'ar_001', name->>'en_US') ILIKE", out)


class SelectingAName(unittest.TestCase):
    """This one does not fail, which is worse."""

    def test_a_bare_translated_column_comes_back_as_json(self):
        # The WHOLE statement, not a substring: the alias used to be glued to
        # FROM (`AS nameFROM account_account`) and a substring check passed.
        self.assertEqual(
            run("SELECT id, name FROM account_account"),
            "SELECT id, name->>'en_US' AS name FROM account_account",
        )

    def test_the_last_item_keeps_its_distance_from_from(self):
        self.assertEqual(
            run("SELECT id, name FROM account_account WHERE name ILIKE '%bank%' LIMIT 3"),
            "SELECT id, name->>'en_US' AS name FROM account_account "
            "WHERE name->>'en_US' ILIKE '%bank%' LIMIT 3",
        )
        self.assertEqual(
            run("SELECT name\nFROM account_account"),
            "SELECT name->>'en_US' AS name\nFROM account_account",
        )

    def test_it_keeps_the_alias_the_query_asked_for(self):
        out = run("SELECT id, name AS account_name FROM account_account")
        self.assertEqual(out, "SELECT id, name->>'en_US' AS account_name FROM account_account")

    def test_a_plain_column_is_left_exactly_as_it_was(self):
        sql = "SELECT id, name, symbol FROM res_currency ORDER BY name"
        self.assertEqual(run(sql), sql)

    def test_ordering_by_a_translated_column_orders_by_the_name(self):
        out = run("SELECT id FROM account_account ORDER BY name DESC")
        self.assertIn("ORDER BY name->>'en_US' DESC", out)

    def test_grouping_too(self):
        out = run("SELECT name, count(*) FROM account_account GROUP BY name")
        self.assertIn("GROUP BY name->>'en_US'", out)


class WhatItRefusesToTouch(unittest.TestCase):
    """A rewrite it is not sure about is a rewrite it must not make."""

    def test_an_unqualified_name_two_tables_share_is_left_alone(self):
        # Postgres would call this ambiguous too; picking one could only ever
        # pick the wrong one.
        sql = ("SELECT id FROM product_template, res_partner WHERE name ILIKE '%x%'")
        self.assertEqual(run(sql), sql)

    def test_a_table_it_knows_nothing_about_is_left_alone(self):
        sql = "SELECT id, name FROM some_customer_table WHERE name ILIKE '%x%'"
        self.assertEqual(run(sql), sql)

    def test_a_string_literal_is_never_rewritten(self):
        sql = ("SELECT id FROM res_partner WHERE ref ILIKE "
               "'%name ILIKE and name->>''en_US''%'")
        self.assertEqual(run(sql), sql)

    def test_a_json_read_already_correct_is_left_alone(self):
        sql = "SELECT id FROM account_account WHERE name->>'en_US' ILIKE '%bank%'"
        self.assertEqual(run(sql), sql)

    def test_a_function_call_is_not_a_column(self):
        sql = "SELECT count(*) FROM account_account WHERE code = '400000'"
        self.assertEqual(run(sql), sql)

    def test_information_schema_queries_are_untouched(self):
        sql = ("SELECT column_name FROM information_schema.columns "
               "WHERE table_name = 'account_account'")
        self.assertEqual(run(sql), sql)


class ReadingTheQuery(unittest.TestCase):
    def test_it_finds_tables_and_their_aliases(self):
        found = dialect.tables_in(
            "SELECT * FROM account_move am JOIN account_move_line AS l ON l.move_id = am.id"
        )
        self.assertEqual(found["am"], "account_move")
        self.assertEqual(found["l"], "account_move_line")
        self.assertEqual(found["account_move"], "account_move")

    def test_a_keyword_is_not_an_alias(self):
        # `FROM account_move WHERE ...` must not read WHERE as an alias, or
        # every unqualified column resolves against a table called "where".
        found = dialect.tables_in("SELECT id FROM account_move WHERE id = 1")
        self.assertNotIn("where", {k.lower() for k in found})
        self.assertIn("account_move", found)

    def test_a_subquery_in_the_select_list_does_not_end_the_list(self):
        out = run("SELECT id, (SELECT count(*) FROM res_partner), name "
                  "FROM account_account")
        self.assertIn("name->>'en_US' AS name", out)
        self.assertIn("(SELECT count(*) FROM res_partner)", out)


if __name__ == "__main__":
    unittest.main()


class QueriesThatAlreadyWork(unittest.TestCase):
    """The rewriter's licence runs out here, and it has to know that.

    A text operator on jsonb cannot execute, so wrapping it can only help. But
    `=` and `IN` DO exist on jsonb -- joining two translated columns is valid
    SQL that runs today -- so rewriting those would break a working query,
    which is the one thing this module must never do.
    """

    def test_joining_two_translated_columns_is_left_alone(self):
        sql = ("SELECT a.id FROM account_account a "
               "JOIN account_tax t ON a.name = t.name")
        self.assertEqual(run(sql), sql)

    def test_in_a_subquery_is_left_alone(self):
        sql = ("SELECT id FROM account_account "
               "WHERE name IN (SELECT name FROM account_tax)")
        self.assertEqual(run(sql), sql)

    def test_equality_to_a_string_is_still_corrected(self):
        # This one cannot execute -- jsonb = text has no operator either.
        out = run("SELECT id FROM account_account WHERE name = 'Bank'")
        self.assertIn("name->>'en_US' = 'Bank'", out)

    def test_in_a_list_of_strings_is_still_corrected(self):
        out = run("SELECT id FROM account_account WHERE name IN ('Bank', 'Cash')")
        self.assertIn("name->>'en_US' IN ('Bank', 'Cash')", out)


class WithAQueryInFront(unittest.TestCase):
    def test_a_cte_does_not_hide_the_outer_select_list(self):
        out = run(
            "WITH recent AS (SELECT id FROM account_move) "
            "SELECT a.id, a.name FROM account_account a "
            "JOIN recent r ON r.id = a.id"
        )
        self.assertIn("a.name->>'en_US' AS name", out)

    def test_the_cte_body_is_not_mistaken_for_the_outer_list(self):
        out = run(
            "WITH names AS (SELECT name FROM account_account) SELECT * FROM names"
        )
        # The outer list is `*`; the CTE's own SELECT is not the outermost one
        # and must not be rewritten as if it were.
        self.assertIn("SELECT * FROM names", out)


class APerCompanyJsonColumn(unittest.TestCase):
    """Odoo 18 keeps `account_account.code` per company in `code_store`, a
    jsonb column keyed by COMPANY ID. Read by language it returns nothing -
    and the query runs, so nothing complains."""

    def test_it_is_read_by_the_connections_company_not_by_language(self):
        self.assertEqual(
            run("SELECT id, code_store, name FROM account_account_v18 WHERE code_store = '101001'",
                company_id=1),
            "SELECT id, code_store->>'1' AS code_store, name->>'en_US' AS name "
            "FROM account_account_v18 WHERE code_store->>'1' = '101001'",
        )

    def test_a_pattern_and_an_order_go_the_same_way(self):
        self.assertEqual(
            run("SELECT id FROM account_account_v18 WHERE code_store LIKE '1010%' ORDER BY code_store",
                company_id=2),
            "SELECT id FROM account_account_v18 WHERE code_store->>'2' LIKE '1010%' "
            "ORDER BY code_store->>'2'",
        )

    def test_a_company_the_agent_named_itself_is_kept(self):
        sql = "SELECT id FROM account_account_v18 WHERE code_store->>'2' = '101001'"
        self.assertEqual(run(sql, company_id=1), sql)

    def test_without_a_company_it_is_left_exactly_as_written(self):
        sql = "SELECT id, code_store FROM account_account_v18 WHERE code_store = '101001'"
        self.assertEqual(run(sql), sql)

    def test_a_translated_column_is_still_read_by_language(self):
        self.assertEqual(
            run("SELECT name FROM account_account_v18 WHERE name ILIKE '%bank%'", company_id=1),
            "SELECT name->>'en_US' AS name FROM account_account_v18 WHERE name->>'en_US' ILIKE '%bank%'",
        )
