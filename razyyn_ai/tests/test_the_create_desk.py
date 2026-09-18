# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""The governed half of recording a document: what the customer's own rules stop.

WHY THESE CASES ARE HERE AND NOT IN THE HTTP HARNESS
    ``tools/create_desk_check.py`` drives the whole desk over real HTTP against
    a running site, which is the only way to catch a route reading the wrong
    parameter or answering in the wrong shape. What it deliberately does not do
    is switch the customer's governance off and on around a live database —
    that belongs in a transaction that rolls back. So the two divide by what
    they can safely touch, not by what they are about.

WHAT IS BEING PROTECTED
    Every case below is a rule the customer set and the agent must not be able
    to talk its way past: writing switched off, check-only mode, a ceiling on
    how much may be recorded in one go. A gate that fails open here is a gate
    that is not there.
"""

from odoo.tests.common import TransactionCase

from ..services import agent_write_service as writes
from ..services.write_guard import AgentWriteError


class CreateDeskTestCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.policy = self.env["razyyn.agent.write.policy"].sudo() \
            .get_policy_singleton()
        self.policy.write({
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

        # A DATABASE WITH NO CHART OF ACCOUNTS IS NOT A FAILURE. Every case
        # below records a journal entry, so it needs a journal and two accounts
        # to record it against — and a site where `account` is installed but no
        # company has been set up yet has neither. Skipping says that; an
        # IndexError in setUp says the module is broken.
        if not self.journal or len(self.accounts) < 2:
            self.skipTest("this database has no chart of accounts to post to")

    def _entry(self, amount=100.0, reference="case"):
        return {
            "journal_id": self.journal.id,
            "date": "2026-09-15",
            "ref": reference,
            "line_ids": [
                {"account_id": self.accounts[0].id, "debit": amount, "credit": 0},
                {"account_id": self.accounts[1].id, "debit": 0, "credit": amount},
            ],
        }

    def _refused(self, documents):
        """Run a batch expecting the whole request to be refused, and return
        the refusal.

        NOT ``assertRaises``. Odoo's own ``assertRaises`` runs the block inside
        a savepoint and rolls back to it, which would make "and nothing was
        written" true no matter what the code did — a gate that fails open
        would still pass. The rollback has to be the service's doing, not the
        test framework's.
        """
        try:
            self._batch(documents)
        except AgentWriteError as exc:
            return exc
        self.fail("this should have been refused, and was not")

    def _batch(self, documents):
        return writes.write_documents_batch(self.env, documents)

    def _one(self, key="k1", amount=100.0, reference="case"):
        return self._batch([{
            "action": "create", "doctype": "account.move", "idempotency_key": key,
            "payload": self._entry(amount, reference),
        }])["results"][0]

    def _log(self, key):
        return self.env["razyyn.agent.write.log"].sudo().search(
            [("idempotency_key", "=", key)], limit=1)

    # ── The receipt has to be enough to find the document ────────────────────

    def test_a_receipt_names_the_document_the_way_the_customers_screen_does(self):
        """A draft journal entry on Odoo has NO number of its own — its `name`
        is "/" until it is posted — and its reference on the wire is a database
        id. A receipt built from that alone said "account.move 126 was
        recorded"; the customer opened Journal Entries, found nothing called
        that, and concluded nothing had been written."""
        row = self._one(key="named", reference="a memorable reference")
        self.assertEqual(row["outcome"], "CREATED")
        self.assertTrue(row["label"])
        self.assertNotEqual(row["label"], row["docname"])

    def test_a_receipt_says_which_company_the_document_landed_in(self):
        """A document lands in whichever company its accounts belong to, which
        need not be the company the customer's screen is showing — and a list
        filtered to another company is empty, which reads exactly like a
        document that was never created."""
        row = self._one(key="placed")
        self.assertEqual(row["company"], self.journal.company_id.name)

    def test_a_repeat_is_named_and_placed_as_the_first_receipt_was(self):
        first = self._one(key="twice")
        second = self._one(key="twice")
        self.assertEqual(second["outcome"], "REPLAYED")
        self.assertEqual(second["docname"], first["docname"])
        self.assertEqual(second["label"], first["label"])
        self.assertEqual(second["company"], first["company"])

    def test_the_recovery_answer_names_the_document_too(self):
        """This is what the agent is told after a request whose reply never
        arrived, and the receipt built from it is the only thing the customer
        will ever see about a document that WAS written."""
        self._one(key="recovered")
        log = writes.get_write_log(self.env, "recovered")
        self.assertEqual(log["status"], "COMMITTED")
        self.assertTrue(log["label"])
        self.assertTrue(log["company"])

    # ── The customer's own rules ─────────────────────────────────────────────

    def test_writing_switched_off_refuses_the_whole_request(self):
        self.policy.enabled = False
        refusal = self._refused([{
            "action": "create", "doctype": "account.move",
            "idempotency_key": "off", "payload": self._entry(reference="switched off"),
        }])
        self.assertEqual(refusal.code, "POLICY_DISABLED")
        self.assertFalse(
            self.env["account.move"].search([("ref", "=", "switched off")]))

    def test_check_only_mode_records_nothing_and_says_so(self):
        """And the log row must NOT read as a success. `get_write_log` decides
        whether a document exists by reading the status alone, so a dry run
        logged as success tells the agent's own recovery path that a document
        was written when none was."""
        self.policy.dry_run_only = True
        row = self._one(key="checkonly")
        self.assertEqual(row["outcome"], "REJECTED")
        self.assertEqual(row["error_code"], "DRY_RUN_ONLY")
        self.assertEqual(self._log("checkonly").status, "rejected")
        self.assertNotEqual(
            writes.get_write_log(self.env, "checkonly")["status"], "COMMITTED")

    def test_a_ceiling_on_documents_stops_the_request_before_anything_lands(self):
        """ONE check for the whole request. Per document it would let a request
        that breaches the ceiling write everything up to the document that
        crosses it — a half-applied import nobody asked for."""
        self.policy.max_documents_per_run = 1
        refusal = self._refused([
            {"action": "create", "doctype": "account.move",
             "idempotency_key": "cap-a", "payload": self._entry(reference="cap a")},
            {"action": "create", "doctype": "account.move",
             "idempotency_key": "cap-b", "payload": self._entry(reference="cap b")},
        ])
        self.assertEqual(refusal.code, "RUN_CAP_DOCUMENTS")
        self.assertFalse(self.env["account.move"].search([("ref", "=", "cap a")]))

    def test_a_ceiling_on_the_amount_stops_it_too(self):
        self.policy.max_total_amount_per_run = 50.0
        refusal = self._refused([{
            "action": "create", "doctype": "account.move",
            "idempotency_key": "cap-amount",
            "payload": self._entry(amount=100.0, reference="over the ceiling"),
        }])
        self.assertEqual(refusal.code, "RUN_CAP_AMOUNT")
        self.assertFalse(
            self.env["account.move"].search([("ref", "=", "over the ceiling")]))

    # ── What a model can and cannot do ───────────────────────────────────────

    def test_a_document_type_with_no_posting_step_says_so(self):
        """It used to try each posting button in turn, find none, and return
        success for a document still sitting in draft. The agent then told the
        customer their entry was posted."""
        partner = self.env["res.partner"].create({"name": "Razyyn case"})
        row = self._batch([{
            "action": "submit", "doctype": "res.partner",
            "docname": str(partner.id), "idempotency_key": "nosubmit",
            "payload": {},
        }])["results"][0]
        self.assertEqual(row["outcome"], "REJECTED")
        self.assertEqual(row["error_code"], "NOT_SUBMITTABLE")

    def test_a_document_type_this_system_does_not_have_is_refused(self):
        row = self._batch([{
            "action": "create", "doctype": "razyyn.no.such.model",
            "idempotency_key": "nomodel", "payload": {},
        }])["results"][0]
        self.assertEqual(row["outcome"], "REJECTED")

    def test_a_posted_document_cannot_be_edited(self):
        created = self._one(key="post-then-edit", reference="to be posted")
        self._batch([{"action": "submit", "doctype": "account.move",
                      "docname": created["docname"], "idempotency_key": "post-it",
                      "payload": {}}])
        row = self._batch([{
            "action": "update", "doctype": "account.move",
            "docname": created["docname"], "idempotency_key": "edit-it",
            "payload": {"ref": "changed after posting"},
        }])["results"][0]
        self.assertEqual(row["error_code"], "NOT_A_DRAFT")


    # ── The field contract the agent composes against ─────────────────────────

    def test_the_spec_publishes_every_field_a_journal_entry_is_made_of(self):
        """THE LOOP THAT LOST A LIVE RUN. The spec dropped every computed
        field without an inverse and every readonly field — which on
        account.move is `date` (stored compute, readonly=False), `move_type`
        (plain readonly with a default) and the line label `name`. The
        platform's own field check then refused every correct journal entry
        ("account.move has no 'date' field"), the agent re-read the live
        schema, saw the field plainly exists, sent the same payload again and
        looped until the customer cancelled. The spec and the database must
        never disagree about a field that is really there."""
        spec = writes.build_document_spec(self.env, "account.move")
        fields = {f["fieldname"]: f for f in spec["fields"]}

        self.assertIn("date", fields)
        self.assertIn("move_type", fields)
        self.assertIn("ref", fields)
        # `move_type` decides what KIND of document a move is; publishing it
        # flagged read-only would tell the agent not to set it.
        self.assertFalse(fields["move_type"]["read_only"])
        # A required field Odoo fills by itself (a default, a compute) is not
        # required OF THE AGENT: demanding `auto_post` made the platform
        # insist on a value Odoo was always going to supply.
        for name in ("auto_post", "date", "journal_id", "currency_id"):
            if name in fields:
                self.assertFalse(fields[name]["reqd"],
                                 f"{name} is filled by Odoo, not the agent")

        lines = next(t for t in spec["child_tables"]
                     if t["fieldname"] == "line_ids")
        line_fields = {f["fieldname"] for f in lines["fields"]}
        self.assertIn("name", line_fields, "the journal item label vanished")
        self.assertIn("account_id", line_fields)
        self.assertIn("debit", line_fields)

    def test_the_spec_never_offers_the_lifecycle_column(self):
        """Lifecycle travels as ACTIONS (submit, cancel) and is read back as
        docstatus. A payload that wrote `state` directly would mark a document
        posted without any of the posting logic running."""
        spec = writes.build_document_spec(self.env, "account.move")
        self.assertNotIn("state", {f["fieldname"] for f in spec["fields"]})

    # ── One request, several documents that depend on each other ─────────────

    def test_a_value_inside_a_payload_may_name_an_earlier_document(self):
        """"Create this supplier and record their invoice" is two documents in
        one request, and the second cannot be written until the first has an
        id."""
        results = self._batch([
            {"action": "create", "doctype": "res.partner", "idempotency_key": "ref-a",
             "payload": {"name": "Razyyn referenced supplier"}},
            {"action": "create", "doctype": "account.move", "idempotency_key": "ref-b",
             "payload": {**self._entry(reference="with a partner"),
                         "partner_id": "@0"}},
        ])["results"]
        self.assertEqual([r["outcome"] for r in results], ["CREATED", "CREATED"])
        move = self.env["account.move"].browse(int(results[1]["docname"]))
        self.assertEqual(move.partner_id.name, "Razyyn referenced supplier")
