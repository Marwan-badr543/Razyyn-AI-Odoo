# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Runs standalone — no Odoo installation required.

Lives OUTSIDE ``razyyn_ai/`` on purpose: that package's own
``__init__.py`` imports ``models``/``controllers``, which import ``odoo``. A
test file placed inside the addon tree gets pulled into that import chain the
moment any test runner resolves its package name — even one that never
touches ``models`` or ``controllers`` itself — because Python (and pytest's
collection) walks up through every ancestor directory that has an
``__init__.py`` to build the dotted module name. Sitting here, one level up,
this file has no such ancestor, so ``python -m pytest`` or
``python -m unittest`` run it in any environment, including one with no Odoo
installed at all — which is exactly what CI does before this addon ever
touches a real Odoo instance.

``razyyn_ai/services/query_guard.py`` itself has zero ``odoo``
imports for the same reason (see its own docstring) — this suite is what
proves that property, by loading it directly from its file path rather than
importing the package around it.

The cases below are drawn from ``razyyn/docs/erp/SQL_GUARD_CONTRACT.md`` and
from ``razyyn/agent/erp/conformance.py``'s corpus, translated to table and
column names that actually exist in an Odoo database — the Frappe corpus's own
literals (``tabAgent Settings``, ``__Auth``, ``tabGL Entry``) do not, and
running them here would prove nothing either way.
"""

import importlib.util
import os
import sys
import unittest

_here = os.path.dirname(os.path.abspath(__file__))
_module_path = os.path.join(
    _here, "..", "razyyn_ai", "services", "query_guard.py"
)
_spec = importlib.util.spec_from_file_location("razyyn_query_guard_under_test", _module_path)
query_guard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = query_guard
_spec.loader.exec_module(query_guard)

ForbiddenQueryError = query_guard.ForbiddenQueryError
assert_query_is_read_only = query_guard.assert_query_is_read_only


def refused(sql: str) -> bool:
    try:
        assert_query_is_read_only(sql)
        return False
    except ForbiddenQueryError:
        return True


class TestWrites(unittest.TestCase):
    def test_update_is_refused(self):
        self.assertTrue(refused("UPDATE account_move SET amount_total = 0"))

    def test_delete_is_refused(self):
        self.assertTrue(refused("DELETE FROM account_move_line WHERE 1=1"))

    def test_drop_is_refused(self):
        self.assertTrue(refused("DROP TABLE account_move_line"))

    def test_insert_is_refused(self):
        self.assertTrue(refused("INSERT INTO res_partner (name) VALUES ('x')"))

    def test_truncate_is_refused_with_no_delete_keyword_present(self):
        self.assertTrue(refused("TRUNCATE TABLE res_partner"))

    def test_create_table_is_refused(self):
        self.assertTrue(refused("CREATE TABLE agent_probe (id int)"))

    def test_grant_is_refused(self):
        self.assertTrue(refused("GRANT ALL ON res_partner TO agent"))


class TestStatementStacking(unittest.TestCase):
    def test_semicolon_stacked_statement_is_refused(self):
        self.assertTrue(refused("SELECT 1; DROP TABLE res_partner"))

    def test_comment_terminated_stack_is_refused(self):
        self.assertTrue(refused("SELECT 1; -- \nDELETE FROM res_partner"))


class TestCredentialAndSecretReads(unittest.TestCase):
    def test_own_settings_table_is_refused(self):
        self.assertTrue(refused("SELECT * FROM razyyn_agent_settings"))

    def test_password_column_is_refused_even_on_a_legitimate_table(self):
        # res_users is a legitimate read — "who confirmed this invoice" — so
        # the block has to be on the column, not the whole table.
        self.assertTrue(refused("SELECT login, password FROM res_users"))

    def test_plain_res_users_read_without_secret_columns_is_allowed(self):
        self.assertFalse(refused("SELECT id, login, name FROM res_users LIMIT 5"))

    def test_oauth_client_secret_table_is_refused(self):
        self.assertTrue(refused("SELECT client_secret FROM auth_oauth_provider"))

    def test_ir_config_parameter_is_refused(self):
        self.assertTrue(refused("SELECT value FROM ir_config_parameter WHERE key = 'database.secret'"))

    def test_mail_server_credentials_are_refused(self):
        self.assertTrue(refused("SELECT smtp_pass FROM ir_mail_server"))
        self.assertTrue(refused("SELECT password FROM fetchmail_server"))

    def test_pg_authid_is_refused(self):
        self.assertTrue(refused("SELECT * FROM pg_authid"))

    def test_mysql_user_table_is_refused_even_though_this_app_runs_on_postgres(self):
        # The contract requires denying both engine families regardless of
        # which one this app actually runs on.
        self.assertTrue(refused("SELECT * FROM mysql.user"))


class TestFilesystemReach(unittest.TestCase):
    def test_load_file_is_refused(self):
        self.assertTrue(refused("SELECT pg_read_file('/etc/passwd')"))

    def test_copy_to_program_is_refused(self):
        self.assertTrue(refused("COPY (SELECT 1) TO PROGRAM 'cat /etc/passwd'"))


class TestFalsePositiveTraps(unittest.TestCase):
    """Cases that must be ALLOWED. A guard that refuses these is too strict —
    the agent cannot answer, blames the ERP, and the accountant is told their
    system is unreachable."""

    def test_a_string_literal_containing_a_forbidden_keyword_is_allowed(self):
        self.assertFalse(
            refused("SELECT name FROM res_partner WHERE name ILIKE '%drop shipping%' LIMIT 1")
        )

    def test_a_cte_is_allowed(self):
        self.assertFalse(refused("WITH x AS (SELECT 1 AS n) SELECT n FROM x"))

    def test_information_schema_columns_is_allowed(self):
        self.assertFalse(
            refused(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'account_move' LIMIT 1"
            )
        )

    def test_information_schema_tables_is_allowed(self):
        self.assertFalse(
            refused("SELECT table_name FROM information_schema.tables LIMIT 1")
        )

    def test_but_information_schema_privileges_is_refused(self):
        self.assertTrue(refused("SELECT * FROM information_schema.table_privileges"))


if __name__ == "__main__":
    unittest.main()
