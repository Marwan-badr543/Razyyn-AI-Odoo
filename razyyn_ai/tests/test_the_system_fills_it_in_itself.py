# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""What this system works out for itself, said plainly, before the agent guesses.

WHAT WENT WRONG, LIVE, ON A TWO-LINE INVOICE
    A customer asked for one sales invoice: a named customer, one item, quantity
    two. The agent read the field list this module publishes, saw twenty
    ordinary-looking fields on the document and sixteen more on each line, and
    went looking for every one of them in the database:

      the customer's receivable account   -> `property_account_receivable_id`
      the customer's payment terms        -> `property_payment_term_id`
      the item's selling price            -> `standard_price`
      the item's tax                      -> `taxes_id`
      the category's income account       -> `property_account_income_categ_id`
      the company's journal               -> `country_id`, `account_journal_id`

    None of those are plain columns. Some are company-dependent values living in
    `ir_property`; some are many-to-many relations; some do not exist at all.
    SEVENTY-THREE queries failed on one invoice, the whole thing took thirteen
    minutes, and the payload it finally produced filled in both
    `invoice_line_ids` AND `line_ids` — the same rows under two names — which
    would have recorded every line twice.

    Every single one of those values Odoo supplies by itself, correctly, the
    moment the item and the customer are named.

THE TWO FACTS THIS MODULE NOW PUBLISHES
    `derived`       — this field is `compute=..., store=True, readonly=False`:
                      the system works it out, and keeps yours if you send one.
    `same_rows_as`  — these two row tables are one set of rows under two names.

Both are read off the model itself, so a document type nobody has looked at yet
is covered the first time it is asked about.
"""

from odoo.tests.common import TransactionCase

from ..services import agent_api_service as reads
from ..services import agent_write_service as writes
from ..services.write_guard import WriteRejectedError


class TheSystemFillsItInItself(TransactionCase):

    def _spec(self, model):
        return writes.build_document_spec(self.env, model)

    def _field(self, spec, name):
        return next(f for f in spec["fields"] if f["fieldname"] == name)

    def _row_field(self, spec, table, name):
        rows = next(t for t in spec["child_tables"] if t["fieldname"] == table)
        return next(f for f in rows["fields"] if f["fieldname"] == name)

    # ── what the system derives ──────────────────────────────────────────────

    def test_a_line_s_account_price_and_tax_are_reported_as_derived(self):
        """The three the agent spent most of its seventy-three queries on."""
        spec = self._spec("account.move")
        for name in ("account_id", "price_unit", "tax_ids"):
            self.assertTrue(
                self._row_field(spec, "invoice_line_ids", name)["derived"],
                f"{name} is worked out from the product; the agent must be told",
            )

    def test_the_document_s_own_journal_and_currency_are_reported_as_derived(self):
        spec = self._spec("account.move")
        for name in ("journal_id", "currency_id", "invoice_payment_term_id"):
            self.assertTrue(self._field(spec, name)["derived"], name)

    def test_a_field_the_agent_must_supply_is_not_reported_as_derived(self):
        """`move_type` decides what KIND of document this is and nothing
        derives it; `partner_id` is the party and nothing derives that either.
        A field wrongly marked derived is one the agent stops supplying."""
        spec = self._spec("account.move")
        for name in ("move_type", "partner_id", "ref"):
            self.assertFalse(self._field(spec, name)["derived"], name)

    def test_derived_is_not_the_same_answer_as_read_only(self):
        """They mean different things and the agent acts differently on each: a
        read-only field is discarded, a derived one takes a value when one is
        sent. A journal entry states its own debit and credit, and those go in."""
        spec = self._spec("account.move")
        debit = self._row_field(spec, "line_ids", "debit")
        self.assertTrue(debit["derived"])
        self.assertFalse(debit["read_only"],
                         "a stated debit must still reach the ledger")

    def test_a_document_type_nobody_taught_it_about_is_covered_too(self):
        """Nothing above names a model. A partner has almost nothing derived
        and that is the right answer for a partner."""
        spec = self._spec("res.partner")
        self.assertFalse(self._field(spec, "name")["derived"])

    # ── two names, one set of rows ───────────────────────────────────────────

    def test_the_two_views_of_an_invoice_s_lines_are_named_as_twins(self):
        spec = self._spec("account.move")
        tables = {t["fieldname"]: t.get("same_rows_as") or []
                  for t in spec["child_tables"]}
        self.assertIn("line_ids", tables["invoice_line_ids"])
        self.assertIn("invoice_line_ids", tables["line_ids"])

    def test_a_row_table_with_no_twin_says_so(self):
        spec = self._spec("account.move")
        payments = next((t for t in spec["child_tables"]
                         if t["fieldname"] == "payment_ids"), None)
        if payments is not None:
            self.assertEqual(payments.get("same_rows_as"), [])

    def test_the_helper_s_own_scratch_key_never_reaches_the_agent(self):
        spec = self._spec("account.move")
        for table in spec["child_tables"]:
            self.assertNotIn("_inverse", table)

    def test_filling_in_both_views_is_refused_rather_than_doubled(self):
        """THE FAILURE THIS PREVENTS. Appending each line twice still balances,
        so nothing downstream would have caught it. Refused, not merged: which
        of the two was meant is not knowable here, and picking one would
        silently discard rows somebody approved."""
        with self.assertRaises(WriteRejectedError) as caught:
            writes._odoo_values(self.env, "account.move", {
                "move_type": "out_invoice",
                "invoice_line_ids": [{"quantity": 1}],
                "line_ids": [{"quantity": 1}],
            })
        said = str(caught.exception)
        self.assertIn("invoice_line_ids", said)
        self.assertIn("line_ids", said)

    def test_one_view_on_its_own_is_accepted(self):
        values = writes._odoo_values(self.env, "account.move", {
            "move_type": "out_invoice",
            "invoice_line_ids": [{"quantity": 1}],
        })
        self.assertEqual(len(values["invoice_line_ids"]), 1)

    def test_two_row_tables_that_are_not_twins_are_both_accepted(self):
        """The guard must fire on the same rows under two names, not on any two
        tables — a document legitimately carries several unrelated ones."""
        values = writes._odoo_values(self.env, "res.partner", {
            "name": "Razyyn twin-table check",
            "child_ids": [{"name": "A contact"}],
            "bank_ids": [{"acc_number": "RZ-TEST-0001"}],
        })
        self.assertEqual(len(values["child_ids"]), 1)
        self.assertEqual(len(values["bank_ids"]), 1)

    # ── where a per-company value actually lives ─────────────────────────────

    def test_a_per_company_value_is_answered_for_however_this_odoo_stores_it(self):
        """WHERE A PARTNER'S RECEIVABLE ACCOUNT LIVES DEPENDS ON THE VERSION,
        and the agent must be told the truth either way.

        Odoo 17 keeps a company-dependent field OUT of the table, as rows in
        `ir_property`. Odoo 18 keeps it ON the table, as a JSON column keyed by
        company id. Both are reported — the first under
        `company_dependent_values` with the properties table named, the second
        as a column with the key explained — and neither is passed over in
        silence, which is what sent the agent guessing
        `property_account_receivable_id`, then `property_account_position_id`,
        then `property_payment_term_id`, each refused in turn.
        """
        summary = reads.build_schema_summary(self.env, "res.partner")
        wanted = "property_account_receivable_id"
        as_a_property = " ".join(summary["company_dependent_values"])
        as_a_column = " ".join(summary["fields"])

        if wanted in as_a_property:
            self.assertIn("ir_property", summary["note"])
            self.assertNotIn(wanted, as_a_column,
                             "it is not a column on this version")
            return

        self.assertIn(wanted, as_a_column, "it is neither, which tells the "
                                           "agent nothing and it will guess")
        line = next(f for f in summary["fields"] if f.startswith(wanted))
        self.assertIn("keyed by COMPANY ID", line)

    def test_a_per_company_json_column_is_not_described_as_a_translated_one(self):
        """THEY ARE READ DIFFERENTLY AND BOTH ARE JSON. A translated column is
        keyed by language; a company-dependent one is keyed by company id.
        Described as translated, a partner's credit limit came back with
        "read it as credit_limit->>'en_US'", which returns nothing at all — the
        keys are "1" and "2". An agent told how to read a field, reading it, and
        getting nothing is worse off than one told nothing."""
        summary = reads.build_schema_summary(self.env, "res.partner")
        for line in summary["fields"]:
            if "keyed by COMPANY ID" in line:
                self.assertNotIn("per language", line)
                self.assertNotIn("stored as JSON per language", line)
                # The line names the language key only to warn the agent OFF
                # it, which is the whole point: the wrong reading is the one it
                # would otherwise reach for.
                self.assertIn("never ->>'en_US'", line)

    def test_a_model_with_no_per_company_values_says_nothing(self):
        summary = reads.build_schema_summary(self.env, "res.currency")
        self.assertEqual(summary["company_dependent_values"], [])


class TwoSetsOfBooksOneName(TransactionCase):
    """A business with two companies has two of most things, with one name each.

    WHAT BROKE WHEN THE AGENT STARTED WRITING NAMES INSTEAD OF IDS
        A plan a customer reads must name records, not database ids — "partner
        14, journal 1, currency 74" is six facts nobody outside the database can
        check, on the one card that exists to be checked. So the desk now writes
        the name and this module resolves it.

        Immediately, live: `journal_id: "Customer Invoices"` was refused —
        "matches more than one of your account.journal records ("Customer
        Invoices", "Customer Invoices")" — and the whole invoice failed. Both
        exist, correctly: one per company. Refusing was right in the absence of
        anything to choose by, and wrong here, because the invoice had already
        said which company it is in.

    A TIE-BREAK, NOT A FILTER. It is reached only after a name has matched more
    than one record, so it can never narrow a search that was going to succeed.
    """

    def setUp(self):
        super().setUp()
        self.companies = self.env["res.company"].search([], limit=2)

    def test_a_name_two_companies_share_resolves_to_this_document_s_company(self):
        if len(self.companies) < 2:
            self.skipTest("this database has only one company")
        first, second = self.companies[0], self.companies[1]
        name = "Razyyn twin-name journal"
        mine = self.env["account.journal"].create([
            {"name": name, "code": "RZT1", "type": "general",
             "company_id": first.id},
            {"name": name, "code": "RZT2", "type": "general",
             "company_id": second.id},
        ])
        for company, journal in zip((first, second), mine):
            values = writes._odoo_values(self.env, "account.move", {
                "move_type": "entry",
                "company_id": company.id,
                "journal_id": name,
            })
            self.assertEqual(
                values["journal_id"], journal.id,
                "the document's own company settles which of the two it means",
            )

    def test_a_name_that_is_still_ambiguous_is_still_refused(self):
        """Two records with one name INSIDE one company is a real question."""
        company = self.companies[0]
        name = "Razyyn same-company twin"
        self.env["account.journal"].create([
            {"name": name, "code": "RZS1", "type": "general",
             "company_id": company.id},
            {"name": name, "code": "RZS2", "type": "general",
             "company_id": company.id},
        ])
        with self.assertRaises(WriteRejectedError):
            writes._odoo_values(self.env, "account.move", {
                "move_type": "entry", "company_id": company.id,
                "journal_id": name,
            })

    def test_a_row_inherits_the_company_its_document_is_in(self):
        """A line does not name a company; the invoice it belongs to does."""
        if len(self.companies) < 2:
            self.skipTest("this database has only one company")
        second = self.companies[1]
        journals = self.env["account.journal"].search(
            [("company_id", "=", second.id), ("type", "=", "general")], limit=1)
        if not journals:
            self.skipTest("no general journal in the second company")
        values = writes._odoo_values(self.env, "account.move", {
            "move_type": "entry",
            "company_id": second.id,
            "journal_id": journals.name,
        })
        self.assertEqual(values["journal_id"], journals.id)

    def test_an_unambiguous_name_is_never_narrowed_by_the_company(self):
        """The company is a DEFAULT, not a filter: a record named exactly once
        is found whichever books it sits in. A customer whose bank account sat
        under their second company was once told it had not been set up."""
        if len(self.companies) < 2:
            self.skipTest("this database has only one company")
        first, second = self.companies[0], self.companies[1]
        only = self.env["account.journal"].create({
            "name": "Razyyn only-in-the-other-company",
            "code": "RZO1", "type": "general", "company_id": second.id,
        })
        values = writes._odoo_values(self.env, "account.move", {
            "move_type": "entry",
            "company_id": first.id,
            "journal_id": only.name,
        })
        self.assertEqual(values["journal_id"], only.id)


class AmbiguityOnAFieldNobodyAskedFor(TransactionCase):
    """A name that repeats, on a field this system was going to fill in anyway.

    THE LIVE RUN THIS CLOSES. Asked for one sales invoice, the agent filled in
    the line's tax by name — "14%" — and this database holds four records with
    that name, two per company. The reference could not be told apart, the
    invoice was rebuilt three times, and it was never recorded at all. The
    customer was sent a table of tax ids instead of their invoice.

    Refusing was right: two taxes are two different figures on a customer's
    bill. But "tell me which one" is the wrong way out of THIS one, because the
    field was never the agent's to send — Odoo derives a line's tax from the
    product and the party, correctly, every time. So the refusal says so.
    """

    def test_an_ambiguous_derived_field_is_told_to_leave_itself_out(self):
        company = self.env["res.company"].search([], limit=1)
        name = "Razyyn twin account"
        accounts = self.env["account.account"]
        shared = ({"company_ids": [(6, 0, company.ids)]}
                  if "company_ids" in accounts._fields
                  else {"company_id": company.id})
        accounts.create([
            {"name": name, "code": "RZTW1", "account_type": "expense", **shared},
            {"name": name, "code": "RZTW2", "account_type": "expense", **shared},
        ])
        with self.assertRaises(WriteRejectedError) as caught:
            writes._odoo_values(self.env, "account.move.line", {
                "name": "a line", "account_id": name,
            })
        said = str(caught.exception)
        self.assertIn("matches more than one", said)
        self.assertIn("leave", said.lower())
        self.assertIn("derived", said.lower())

    def test_an_ambiguous_field_the_agent_must_supply_says_no_such_thing(self):
        """A party is the agent's to name, so "which one did you mean" is the
        whole of the right answer there."""
        name = "Razyyn twin partner"
        self.env["res.partner"].create([{"name": name}, {"name": name}])
        with self.assertRaises(WriteRejectedError) as caught:
            writes._odoo_values(self.env, "account.move", {
                "move_type": "out_invoice", "partner_id": name,
            })
        said = str(caught.exception)
        self.assertIn("matches more than one", said)
        self.assertNotIn("leave", said.lower())
