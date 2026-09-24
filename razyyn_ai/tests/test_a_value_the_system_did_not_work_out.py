# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""A field this system was going to fill in, that it did not fill in.

WHAT HAPPENED THREE TIMES, ON TWO ODOO VERSIONS, BEFORE THIS EXISTED
    A customer asked for an ordinary miscellaneous journal entry — "debit
    Prepaid Expenses 500, credit Main Safe 500", and later "I bought a building
    for 5,000,000 cash". Each time the entry reached Odoo with two lines
    carrying a label, a debit and a credit and NO ACCOUNT, and each time the
    write died inside PostgreSQL on the check constraint that says an
    accountable line must have one. A CheckViolation is not a `UserError`, so
    what the customer read was that their system "could not process" the
    document and that the details were in a log for their administrator. The
    approval card they had just said yes to showed two lines that named no
    account at all — which is what "debit and credit are the same account"
    looks like from the outside.

WHY THE PUBLISHED FIELD LIST CANNOT FIX IT
    `account.move.line.account_id` is `compute=..., store=True,
    readonly=False`, so this module publishes it as DERIVED, and
    `test_the_system_fills_it_in_itself` is right to insist on that: on an
    invoice line Odoo works the account out from the product, and an agent that
    goes looking for it instead spends seventy-three failed queries finding
    nothing. On a line in a general journal the same compute's last resort is
    the journal's own default account — and a Miscellaneous journal has none.

    One field, one model, two opposite truths, decided by the rest of the
    document. The spec is per-model, so no flag on it can say both.

SO IT IS ASKED, NOT DECLARED
    Preflight already builds the document in memory, which runs every compute
    Odoo would run. The fields the model's own CHECK constraints say must not
    be null are then read back off that trial record: still empty means Odoo
    was never going to supply one, and the agent is told so while nothing has
    been written.
"""

from odoo.tests.common import TransactionCase

from ..services import agent_write_service as writes


class AValueTheSystemDidNotWorkOut(TransactionCase):

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
        self.journal = self.env["account.journal"].search(
            [("type", "=", "general")], limit=1)
        self.accounts = self.env["account.account"].search(
            [("company_ids", "in", self.journal.company_id.ids)]
            if "company_ids" in self.env["account.account"]._fields
            else [("company_id", "=", self.journal.company_id.id)], limit=2
        ) if self.journal else self.env["account.account"]
        if not self.journal or len(self.accounts) < 2:
            self.skipTest("this database has no chart of accounts to post to")

    def _findings(self, payload):
        return writes.preflight_document(self.env, payload)["findings"]

    def _entry(self, lines):
        return {
            "doctype": "account.move",
            "move_type": "entry",
            "journal_id": self.journal.id,
            "date": "2026-09-23",
            "ref": "Office supplies",
            "line_ids": lines,
        }

    # ── the refusal the customer actually met ────────────────────────────────

    def test_a_journal_entry_whose_lines_name_no_account_is_stopped(self):
        """THE BUG. Both lines, named by the row number on the customer's own
        screen, before anything is written."""
        findings = self._findings(self._entry([
            {"name": "Office supplies", "debit": 500.0},
            {"name": "Office supplies", "credit": 500.0},
        ]))
        paths = [f["field_path"] for f in findings]
        self.assertEqual(paths, ["line_ids[1].account_id",
                                 "line_ids[2].account_id"])
        for finding in findings:
            self.assertEqual(finding["severity"], "BLOCKING")
            self.assertIn("Account", finding["human_message"])
            self.assertIn("account.account", finding["human_message"],
                          "the agent has to be told where to find one")

    def test_the_same_entry_with_its_accounts_named_passes(self):
        self.assertEqual(self._findings(self._entry([
            {"name": "Office supplies", "debit": 500.0,
             "account_id": self.accounts[0].id},
            {"name": "Office supplies", "credit": 500.0,
             "account_id": self.accounts[1].id},
        ])), [])

    # ── the same miss, on a journal that HAS a fallback ──────────────────────

    def test_every_line_on_one_account_by_accident_is_stopped(self):
        """THE HALF THAT DOES NOT ANNOUNCE ITSELF. Give the journal a default
        account and Odoo's compute stops leaving the line empty — it fills
        EVERY line with that one account. The entry balances, the constraint
        is satisfied, nothing refuses it, and the ledger gets a debit and a
        credit against the same account: the movement of nothing."""
        self.journal.default_account_id = self.accounts[0]
        findings = self._findings(self._entry([
            {"name": "Office supplies", "debit": 500.0},
            {"name": "Office supplies", "credit": 500.0},
        ]))
        self.assertEqual([f["field_path"] for f in findings], ["line_ids"])
        said = findings[0]["human_message"]
        self.assertIn("same account", said)
        self.assertIn(self.accounts[0].display_name, said)

    def test_a_same_account_entry_somebody_ASKED_for_is_left_alone(self):
        """Two lines on one account is a real thing to want — a
        reclassification, a storno correction. The difference is whether
        anybody chose it, and the payload says so by naming the account."""
        self.journal.default_account_id = self.accounts[0]
        self.assertEqual(self._findings(self._entry([
            {"name": "Reclassification", "debit": 500.0,
             "account_id": self.accounts[0].id},
            {"name": "Reclassification", "credit": 500.0,
             "account_id": self.accounts[0].id},
        ])), [])

    def test_two_different_accounts_are_what_a_correct_entry_looks_like(self):
        self.journal.default_account_id = self.accounts[0]
        self.assertEqual(self._findings(self._entry([
            {"name": "Office supplies", "debit": 500.0,
             "account_id": self.accounts[0].id},
            {"name": "Office supplies", "credit": 500.0,
             "account_id": self.accounts[1].id},
        ])), [])

    # ── and what must NOT start being asked for ──────────────────────────────

    def test_an_invoice_line_carrying_only_a_product_is_left_alone(self):
        """THE REGRESSION THIS CHECK COULD EASILY HAVE BEEN. Here the account
        IS derived — from the product — and demanding one would send the desk
        back to looking up receivable accounts and income accounts by hand.
        The check asks Odoo what it actually produced, so it stays quiet."""
        product = self.env["product.product"].search(
            [("sale_ok", "=", True)], limit=1)
        partner = self.env["res.partner"].search([("customer_rank", ">", 0)],
                                                 limit=1)
        if not product or not partner:
            self.skipTest("this database has no product or customer to invoice")
        self.assertEqual(self._findings({
            "doctype": "account.move",
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "invoice_line_ids": [{"product_id": product.id, "quantity": 2}],
        }), [])

    def test_a_section_heading_is_exempt_because_the_constraint_says_so(self):
        """A section carries no account and never should. The exemption is read
        off the constraint itself — `display_type IN ('line_section',
        'line_note') OR account_id IS NOT NULL` — rather than from this
        connector knowing what Odoo calls a heading."""
        product = self.env["product.product"].search(
            [("sale_ok", "=", True)], limit=1)
        partner = self.env["res.partner"].search([("customer_rank", ">", 0)],
                                                 limit=1)
        if not product or not partner:
            self.skipTest("this database has no product or customer to invoice")
        self.assertEqual(self._findings({
            "doctype": "account.move",
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "invoice_line_ids": [
                {"display_type": "line_section", "name": "Part A"},
                {"product_id": product.id, "quantity": 2},
            ],
        }), [])

    # ── how much of a constraint this reader is willing to believe ───────────

    def test_the_constraint_that_names_the_account_is_read_with_its_exemption(self):
        wanted = writes._constraint_requirements(self.env["account.move.line"])
        self.assertIn("account_id", wanted)
        self.assertEqual(
            wanted["account_id"],
            ((("display_type", True, frozenset({"line_section", "line_note"})),),),
        )

    def test_a_constraint_offering_a_CHOICE_is_not_read_as_a_requirement(self):
        """`sale.order.line` accepts a display type OR a product and a unit.
        Both sides say IS NOT NULL, so neither is required — and a reader that
        took the first one would demand a section heading on every order line.
        The shape is left alone rather than guessed at."""
        if "sale.order.line" not in self.env:
            self.skipTest("sales is not installed here")
        self.assertEqual(
            writes._constraint_requirements(self.env["sale.order.line"]), {})

    def test_a_constraint_written_as_a_NEGATION_is_left_alone(self):
        """`NOT (x IS NOT NULL AND y IS NULL)` means x is OPTIONAL. Read
        naively it says the opposite, so anything with a negation or an
        `IS NULL` in it is skipped whole."""
        for model in ("ir.filters", "discuss.channel.member"):
            if model in self.env:
                self.assertEqual(
                    writes._constraint_requirements(self.env[model]), {},
                    f"{model} has no requirement this reader can state")

    def test_a_model_with_no_such_constraint_asks_for_nothing(self):
        self.assertEqual(
            writes._constraint_requirements(self.env["account.move"]), {})

    # ── and if one ever gets past preflight anyway ───────────────────────────

    def test_a_database_rule_is_refused_in_this_system_s_own_words(self):
        """THE OTHER HALF OF WHAT THE CUSTOMER READ. Odoo states this rule as a
        PostgreSQL constraint and writes a sentence beside it; psycopg2 raises,
        so the sentence never reached anybody and the customer was sent to read
        a log instead."""
        policy = self.env["razyyn.agent.write.policy"].sudo() \
            .get_policy_singleton()
        policy.write({"enabled": True, "dry_run_only": False,
                      "restrict_to_listed_models": False,
                      "max_documents_per_run": 0,
                      "max_total_amount_per_run": 0.0})
        result = writes.write_documents_batch(self.env, [{
            "action": "create", "doctype": "account.move",
            "idempotency_key": "razyyn-no-account-case",
            "payload": {
                "move_type": "entry", "journal_id": self.journal.id,
                "date": "2026-09-23", "ref": "no account",
                "line_ids": [{"name": "x", "debit": 500.0},
                             {"name": "x", "credit": 500.0}],
            },
        }])["results"][0]
        self.assertEqual(result["outcome"], "REJECTED")
        # ODOO'S SENTENCE, WORD FOR WORD, and nothing looser. "account" appears
        # in a policy refusal naming account.move too, so a substring check
        # here would pass on a refusal that never reached the constraint at
        # all — and this case exists precisely to prove that it did.
        self.assertEqual(result["error_message"],
                         "Missing required account on accountable line.")

    def test_a_crash_with_no_such_sentence_still_says_nothing_technical(self):
        """The fallback has not been removed: a failure that is not a named
        constraint is still a crash, and its text is still kept away from the
        customer."""
        self.assertEqual(
            writes._their_own_words_for(self.env, ValueError("boom")), "")
        self.assertIn("could not process",
                      writes._could_not_process("account.move"))
