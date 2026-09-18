# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Unit tests for Write Guard safety controls."""

import importlib.util
import os
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock

# Mock odoo if not installed (for standalone test execution)
if "odoo" not in sys.modules:
    mock_odoo = MagicMock()
    mock_odoo_exceptions = MagicMock()
    sys.modules["odoo"] = mock_odoo
    sys.modules["odoo.exceptions"] = mock_odoo_exceptions

_here = os.path.dirname(os.path.abspath(__file__))
_module_path = os.path.join(
    _here, "..", "razyyn_ai", "services", "write_guard.py"
)
_spec = importlib.util.spec_from_file_location("razyyn_write_guard_under_test", _module_path)
write_guard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = write_guard
_spec.loader.exec_module(write_guard)

_link_spec = importlib.util.spec_from_file_location(
    "razyyn_link_match_under_test",
    os.path.join(_here, "..", "razyyn_ai", "services", "link_match.py"),
)
link_match = importlib.util.module_from_spec(_link_spec)
sys.modules[_link_spec.name] = link_match
_link_spec.loader.exec_module(link_match)

AgentWriteError = write_guard.AgentWriteError
WritePolicyDisabledError = write_guard.WritePolicyDisabledError
ModelNotAllowedError = write_guard.ModelNotAllowedError
PolicyCapExceededError = write_guard.PolicyCapExceededError
AccountBlockedError = write_guard.AccountBlockedError
PostingDateForbiddenError = write_guard.PostingDateForbiddenError
check_write_policy = write_guard.check_write_policy
validate_ledger_move_balance = write_guard.validate_ledger_move_balance


class MockModel:
    def __init__(self, model):
        self.model = model


class MockAccount:
    def __init__(self, acc_id, code):
        self.id = acc_id
        self.code = code


class MockPolicy:
    def __init__(self):
        self.enabled = True
        self.restrict_to_listed_models = True
        self.allowed_model_ids = [MockModel("account.move"), MockModel("res.partner")]
        self.posting_date_max_days_forward = 30
        self.posting_date_max_days_back = 30
        self.max_total_amount_per_run = 10000.0
        mock_acc_set = MagicMock()
        mock_acc_set.ids = [99]
        self.blocked_account_ids = mock_acc_set



class TestWriteGuard(unittest.TestCase):
    def setUp(self):
        self.policy = MockPolicy()
        self.env = MagicMock()
        self.env["razyyn.agent.write.policy"].sudo().get_policy_singleton.return_value = self.policy

    def test_disabled_policy_raises(self):
        self.policy.enabled = False
        with self.assertRaises(WritePolicyDisabledError):
            check_write_policy(self.env, "account.move", {})

    def test_unlisted_model_raises(self):
        with self.assertRaises(ModelNotAllowedError):
            check_write_policy(self.env, "ir.cron", {})

    def test_allowed_model_passes(self):
        try:
            check_write_policy(self.env, "account.move", {"invoice_date": date.today().isoformat(), "amount_total": 500.0})
        except Exception as exc:
            self.fail(f"check_write_policy raised unexpectedly: {exc}")

    def test_out_of_bounds_date_raises(self):
        with self.assertRaises(PostingDateForbiddenError):
            check_write_policy(self.env, "account.move", {"invoice_date": "2020-01-01"})


    def test_exceeded_amount_cap_raises(self):
        with self.assertRaises(PolicyCapExceededError):
            check_write_policy(self.env, "account.move", {"amount_total": 15000.0})

    def test_blocked_account_raises(self):
        with self.assertRaises(AccountBlockedError):
            check_write_policy(
                self.env,
                "account.move",
                {
                    "line_ids": [
                        (0, 0, {"account_id": 99, "debit": 100, "credit": 0}),
                        (0, 0, {"account_id": 10, "debit": 0, "credit": 100}),
                    ]
                },
            )

    def test_unbalanced_journal_entry_raises(self):
        with self.assertRaises(AgentWriteError):
            validate_ledger_move_balance(
                "account.move",
                {
                    "move_type": "entry",
                    "line_ids": [
                        (0, 0, {"debit": 100, "credit": 0}),
                        (0, 0, {"debit": 0, "credit": 50}),
                    ],
                },
            )

    def test_balanced_journal_entry_passes(self):
        try:
            validate_ledger_move_balance(
                "account.move",
                {
                    "move_type": "entry",
                    "line_ids": [
                        (0, 0, {"debit": 100, "credit": 0}),
                        (0, 0, {"debit": 0, "credit": 100}),
                    ],
                },
            )
        except Exception as exc:
            self.fail(f"validate_ledger_move_balance raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main()


class LinkTargetNaming(unittest.TestCase):
    """Which record a name means, and when that is genuinely a question.

    `name_search` matches on a substring, so "Bank" on a real chart of accounts
    comes back with "Bank", "Bank Charges" and "Bank Suspense Account". An
    earlier version called that ambiguous and refused it -- so the agent asked
    the customer which "Bank" they meant, about the account they had just named
    exactly. Refusing a genuine tie is right; refusing an exact name is a
    clarification loop on every ordinary account.

    THE REAL FUNCTION, not a copy of its rule: a test that re-implements what it
    is checking passes for ever, including after the original changes.
    """

    @staticmethod
    def _pick(text, matches):
        found = link_match.pick_link_match(text, matches)
        if isinstance(found, int):
            return found
        if not found:
            return "NOT_FOUND"
        return "AMBIGUOUS"

    def test_an_exact_name_wins_over_its_longer_neighbours(self):
        self.assertEqual(
            self._pick("Bank", [(1, "Bank"), (2, "Bank Charges"), (3, "Bank Suspense")]),
            1,
        )

    def test_case_and_spacing_do_not_make_it_ambiguous(self):
        self.assertEqual(self._pick("bank ", [(1, "Bank"), (2, "Bank Charges")]), 1)

    def test_a_real_tie_is_still_a_question(self):
        self.assertEqual(self._pick("Bank", [(1, "Bank"), (2, "Bank")]), "AMBIGUOUS")

    def test_nothing_matching_is_refused(self):
        self.assertEqual(self._pick("Nowhere", []), "NOT_FOUND")

    def test_one_partial_match_is_taken(self):
        self.assertEqual(self._pick("Suspense", [(3, "Bank Suspense Account")]), 3)


class DisplayedNamesResolve(unittest.TestCase):
    """A reference written the way the screen shows it must still find its record.

    Odoo builds a journal entry's displayed name from two columns —
    ``MISC/2026/09/0004 (RAZYYN-TEST-B)`` is the number and the reference — and
    its own ``name_search`` matches either column ALONE, never the string it
    just produced. So the loop did not close: this module told the agent a
    document was called that, the agent named it back exactly as told, and the
    module answered that no such record existed. It is also what an accountant
    copies off their own screen.
    """

    def test_a_plain_reference_is_tried_as_written_and_nothing_else(self):
        self.assertEqual(link_match.spellings_of("MISC/2026/09/0004"),
                         ["MISC/2026/09/0004"])

    def test_a_displayed_name_falls_back_to_its_parts(self):
        self.assertEqual(
            link_match.spellings_of("MISC/2026/09/0004 (RAZYYN-TEST-B)"),
            ["MISC/2026/09/0004 (RAZYYN-TEST-B)", "MISC/2026/09/0004", "RAZYYN-TEST-B"],
        )

    def test_the_literal_spelling_always_comes_first(self):
        """A record genuinely named "Acme (Holdings)" must match itself before
        anything is retried on a fragment of its name."""
        self.assertEqual(link_match.spellings_of("Acme (Holdings)")[0], "Acme (Holdings)")

    def test_brackets_that_are_not_a_suffix_are_left_alone(self):
        self.assertEqual(link_match.spellings_of("Bank (USD) account"),
                         ["Bank (USD) account"])

    def test_nothing_is_nothing(self):
        self.assertEqual(link_match.spellings_of(""), [])
        self.assertEqual(link_match.spellings_of(None), [])
