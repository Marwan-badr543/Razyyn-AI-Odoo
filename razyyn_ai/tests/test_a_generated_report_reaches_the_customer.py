# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""A report the agent generates, from the upload that stores it to the link
that opens it.

THREE THINGS STOOD BETWEEN A GENERATED FILE AND THE PERSON WHO ASKED FOR IT,
and each of them on its own was enough to lose the file. They are tested here
together because fixing any one of them alone still ends with "it cannot
generate files" -- which is exactly what happened, twice, while (1) below was
being got right.

  1. THE UPLOAD WAS REFUSED, AND IT TOOK THREE TRIES TO SEE WHY. The guard
     first compared the conversation's ``agent_settings_id`` against the
     calling connection, and NOTHING EVER WRITES THAT FIELD -- the chat creates
     a conversation with an owner and no connection at all. Every customer got
     "Chat session '<their own id>' not found".

     Comparing the two OWNERS instead, which is what the Frappe app does,
     refused everybody just the same: ``ensure_agent_credentials`` issues one
     key for the whole database through ``sudo()``, so that key belongs to
     Odoo's ROOT USER and never to an accountant.

     And exempting root-owned keys was still not enough. On the customer's own
     two databases the key sat on a PER-PERSON row belonging to a third Odoo
     user, while the conversation belonged to the administrator -- and those
     two rows carried the same ``erp_connection_id``. WHICH ROW HOLDS THE KEY
     IS NOT A FACT ABOUT WHO IS CHATTING. The two sides are matched on the
     Razyyn connection they share; a different one is still refused.

  2. THE ADDRESS HANDED BACK WAS NOT ONE THIS SITE SERVES. The reply carried
     ``/razyyn/chat/download_file?attachment_id=7``; the route is declared with
     the id in the PATH, and a path with no id segment matches no route. The
     chip appeared in the chat and led to an Odoo 404.

  3. THE OWNER WAS REFUSED THEIR OWN REPORT. The route asked "did you create
     this file". A generated report is created by the service account, because
     the caller that uploads it is an API key rather than a person.

The Frappe app's ``_assert_session_owned_by`` compares PEOPLE, and is right to:
there an Agent Settings belongs to whoever made it. Here it does not, which is
the whole of (1).
"""

import base64
from unittest.mock import patch

from odoo import http
from odoo.tests.common import TransactionCase

from ..controllers import chat_client_api
from ..services import agent_api_service as svc


class _Request:
    """Just enough of ``odoo.http.request`` for the download route to run."""

    def __init__(self, env):
        self.env = env
        self.served = None

    def make_response(self, data, headers=None):
        self.served = (data, dict(headers or []))
        return http.Response(data, headers=headers)


class _AGeneratedReport(TransactionCase):

    def setUp(self):
        super().setUp()
        self.sessions = self.env["razyyn.agent.chat.session"].sudo()
        self.settings = self.env["razyyn.agent.settings"].sudo()
        self.accountant = self.env["res.users"].sudo().create({
            "name": "The accountant", "login": "generated.report.owner@test.local",
        })
        self.somebody_else = self.env["res.users"].sudo().create({
            "name": "Somebody else", "login": "generated.report.other@test.local",
        })

    def _conversation_of(self, user, session_id):
        """A conversation created the way the chat itself creates one.

        `create_chat_with_id` records the person and no connection -- which is
        exactly the row the old guard could never match.
        """
        return self.sessions.create({
            "session_id": session_id, "title": "New Chat", "user_id": user.id,
        })

    def _connection_of(self, user, connection=None):
        """One `razyyn.agent.settings` row, optionally on a named Razyyn
        connection -- `erp_connection_id` is the platform's id for "this Odoo,
        linked to Razyyn", and several Odoo users share one of those."""
        return self.settings.create({
            "label": f"Connection of {user.name}",
            "user_id": user.id,
            "company_id": self.env.company.id,
            "erp_connection_id": connection or False,
        })

    # ── 1. the upload ───────────────────────────────────────────────────────

    def test_the_owner_of_the_conversation_may_store_a_report(self):
        """The production failure, byte for byte: a conversation with no
        connection recorded on it, and the key of the person who owns it."""
        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-000000000001")
        self.assertFalse(session.agent_settings_id,
                         "the chat records no connection -- that is the point")

        stored = svc.save_generated_file(
            self.env, session.session_id, "Trial Balance.pdf", b"%PDF-1.4 fake",
            self._connection_of(self.accountant),
        )

        self.assertTrue(stored["success"])
        attachment = self.env["ir.attachment"].sudo().browse(stored["attachment_id"])
        self.assertEqual(attachment.res_model, "razyyn.agent.chat.session")
        self.assertEqual(attachment.res_id, session.id)
        self.assertFalse(attachment.public, "these are financial working papers")
        self.assertEqual(base64.b64decode(attachment.datas), b"%PDF-1.4 fake")

    def test_the_sites_own_key_reaches_the_accountants_conversation(self):
        """The production failure after the first fix. The key the platform
        calls this Odoo back with is made by `ensure_agent_credentials` under
        `sudo()`, so it belongs to Odoo's root user -- to nobody -- while the
        conversation belongs to a person. Holding those two to be equal refused
        every upload on the customer's own site. That row names no Razyyn
        connection either, so it is the site's own credential and reaches its
        own database."""
        from ..services import agent_connection_service as connection

        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-00000000000f")
        the_sites_key, _plaintext = connection.ensure_agent_credentials(self.env)
        self.assertNotEqual(the_sites_key.user_id.id, self.accountant.id)

        stored = svc.save_generated_file(
            self.env, session.session_id, "Trial Balance.pdf", b"%PDF-1.4 fake",
            the_sites_key)
        self.assertEqual(
            self.env["ir.attachment"].sudo().browse(stored["attachment_id"]).res_id,
            session.id)

    def test_a_second_odoo_user_on_the_same_razyyn_connection_is_the_customer(self):
        """THE SHAPE THAT BROKE IT ON THE CUSTOMER'S OWN TWO DATABASES.

        The key the platform calls with sat on a connection row belonging to a
        DIFFERENT Odoo user from the one whose conversation it was, and the two
        rows carried the same `erp_connection_id` -- one Razyyn connection,
        several Odoo people sharing it. Comparing the two OWNERS refused every
        upload; matching the connection they share accepts exactly this and
        nothing wider.
        """
        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-000000000010")
        self._connection_of(self.accountant, connection="the-customers-razyyn-connection")
        somebody_elses_row = self._connection_of(
            self.somebody_else, connection="the-customers-razyyn-connection")

        stored = svc.save_generated_file(
            self.env, session.session_id, "Ledger.xlsx", b"x", somebody_elses_row)
        self.assertEqual(
            self.env["ir.attachment"].sudo().browse(stored["attachment_id"]).res_id,
            session.id)

    def test_another_razyyn_connection_is_not_found(self):
        """And the boundary that is kept. A second Razyyn account connected to
        the same Odoo carries its own `erp_connection_id`, and reaches none of
        this customer's conversations. Refused as NOT FOUND rather than as
        forbidden, so the answer cannot be used to discover session ids."""
        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-000000000002")
        self._connection_of(self.accountant, connection="the-customers-razyyn-connection")
        a_different_customer = self._connection_of(
            self.somebody_else, connection="somebody-elses-razyyn-connection")

        with self.assertRaises(svc.ResourceNotFoundError):
            svc.save_generated_file(
                self.env, session.session_id, "Ledger.xlsx", b"x", a_different_customer)

    def test_a_personal_key_that_never_reached_razyyn_stays_with_its_owner(self):
        """A connection row that has never been linked to the platform names no
        connection to match on. Held to its own owner, which is all that can
        honestly be said about it."""
        theirs = self._conversation_of(self.somebody_else, "aaaaaaaa-0000-0000-0000-000000000011")
        with self.assertRaises(svc.ResourceNotFoundError):
            svc.session_this_key_may_write_to(
                self.env, theirs.session_id, self._connection_of(self.accountant))

    def test_a_conversation_that_does_not_exist_is_refused_and_not_invented(self):
        """It used to be created. A row invented here is owned by the service
        account and takes the id the chat is about to ask for -- and
        `create_chat_with_id` refuses an id that already exists, so the
        customer's own conversation could never be opened."""
        unknown = "aaaaaaaa-0000-0000-0000-000000000003"
        with self.assertRaises(svc.ResourceNotFoundError):
            svc.save_generated_file(
                self.env, unknown, "Ledger.xlsx", b"x",
                self._connection_of(self.accountant),
            )
        self.assertFalse(self.sessions.search([("session_id", "=", unknown)]))

    def test_two_empty_hands_do_not_match_each_other(self):
        """The trap in the port. Two empty recordsets compare EQUAL to one
        another, so a guard written as `session.user_id != settings.user_id`
        PASSES when neither side has an owner -- the one boundary stopping a
        key from reaching another customer's conversation becomes no boundary
        at all. Each side is checked for being there before they are compared,
        which is the shape the Frappe app's guard also has.

        Reached here with a conversation that does not exist and a connection
        that was never resolved: both sides empty, nothing equal to anything.
        """
        nobodys = self.settings.browse(())
        with self.assertRaises(svc.ResourceNotFoundError):
            svc.session_this_key_may_write_to(
                self.env, "aaaaaaaa-0000-0000-0000-00000000000a", nobodys)

    def test_a_connection_with_no_owner_reaches_nothing(self):
        """A real conversation, and a caller that resolves to no person."""
        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-000000000004")
        with self.assertRaises(svc.ResourceNotFoundError):
            svc.session_this_key_may_write_to(self.env, session.session_id, self.settings.browse(()))
        with self.assertRaises(svc.ResourceNotFoundError):
            svc.session_this_key_may_write_to(self.env, session.session_id, None)

    def test_a_missing_session_id_is_named_as_the_missing_parameter(self):
        with self.assertRaises(svc.MissingParameterError):
            svc.save_generated_file(
                self.env, "", "Ledger.xlsx", b"x", self._connection_of(self.accountant))

    def test_a_report_larger_than_the_ceiling_is_refused(self):
        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-000000000005")
        too_big = b"0" * (svc.MAX_GENERATED_FILE_BYTES + 1)
        with self.assertRaises(svc.FileTooLargeError):
            svc.save_generated_file(
                self.env, session.session_id, "Huge.csv", too_big,
                self._connection_of(self.accountant))

    def test_a_conversation_whose_id_was_rotated_is_still_found(self):
        """Editing a message gives the conversation a NEW id upstream.

        Every turn is sent to the agent under `get_backend_session_id()`, and
        an edit rotates it so the desk starts clean. From then on the agent
        asks for its files under the rotated id -- which `session_id` does not
        hold. Without this, one edited message put the customer straight back
        on "Chat session '<id>' not found", for the same report.
        """
        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-00000000000c")
        session.write({"backend_session_id": "bbbbbbbb-0000-0000-0000-00000000000c"})

        stored = svc.save_generated_file(
            self.env, session.backend_session_id, "Ageing.xlsx", b"x",
            self._connection_of(self.accountant))
        self.assertEqual(
            self.env["ir.attachment"].sudo().browse(stored["attachment_id"]).res_id,
            session.id, "the report must land on the conversation, not on a new row")

    def test_the_conversations_own_id_wins_over_a_rotated_one(self):
        """A rotated id is a forwarding address, not an identity. If one row
        happens to carry another row's id in that column, the row whose OWN id
        it is must be the one found -- otherwise the forwarding address decides
        who owns a file."""
        mine = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-00000000000d")
        theirs = self._conversation_of(self.somebody_else, "aaaaaaaa-0000-0000-0000-00000000000e")
        theirs.write({"backend_session_id": mine.session_id})

        found = svc.session_this_key_may_write_to(
            self.env, mine.session_id, self._connection_of(self.accountant))
        self.assertEqual(found, mine)

    # ── 2. the address ──────────────────────────────────────────────────────

    def test_the_address_handed_back_is_one_this_site_serves(self):
        """Not a string comparison: the reply's address is matched against the
        route's OWN declared paths. An address this site does not serve is a
        chip that opens an Odoo 404, which is what the customer saw."""
        from werkzeug.routing import Map, Rule

        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-000000000006")
        stored = svc.save_generated_file(
            self.env, session.session_id, "Statement.pdf", b"%PDF-1.4 fake",
            self._connection_of(self.accountant))

        declared = chat_client_api.RazyynChatApi.download_file.original_routing["routes"]
        routes = Map([Rule(path, endpoint="download") for path in declared]).bind("example.com")
        path, query = (stored["file_url"].split("?") + [""])[:2]
        routes.match(path, query_args=query)  # raises NotFound if no route serves it

    def test_a_report_can_still_be_sent_from_the_address_it_is_given(self):
        """The generated report is also what the customer asks to be e-mailed
        or sent on WhatsApp, and messaging resolves the file from its address.
        An address the sender cannot read is a report that cannot be sent."""
        from ..services import agent_messaging_service as messaging

        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-000000000007")
        stored = svc.save_generated_file(
            self.env, session.session_id, "Statement.pdf", b"%PDF-1.4 fake",
            self._connection_of(self.accountant))

        self.assertEqual(messaging._attachment_id(stored["file_url"]), stored["attachment_id"])
        # And the address reports were handed out under before this was
        # reconciled still resolves, so one already in a transcript still sends.
        self.assertEqual(
            messaging._attachment_id(
                f"/razyyn/chat/download_file?attachment_id={stored['attachment_id']}"),
            stored["attachment_id"])

    # ── 3. the download ─────────────────────────────────────────────────────

    def _may_read(self, attachment, user):
        request = _Request(self.env(user=user))
        with patch.object(chat_client_api, "request", request):
            return chat_client_api._the_caller_may_read(attachment)

    def test_the_owner_may_open_a_report_they_did_not_create(self):
        """The other half of the bug. A generated report is created by the
        service account -- the API key is not a person -- so a route that asks
        only "did you create this" refuses the customer their own report."""
        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-000000000008")
        stored = svc.save_generated_file(
            self.env, session.session_id, "Ageing.xlsx", b"x",
            self._connection_of(self.accountant))
        attachment = self.env["ir.attachment"].sudo().browse(stored["attachment_id"])

        self.assertNotEqual(attachment.create_uid.id, self.accountant.id)
        self.assertTrue(self._may_read(attachment, self.accountant))

    def test_nobody_else_may_open_it(self):
        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-000000000009")
        stored = svc.save_generated_file(
            self.env, session.session_id, "Ageing.xlsx", b"x",
            self._connection_of(self.accountant))
        attachment = self.env["ir.attachment"].sudo().browse(stored["attachment_id"])

        self.assertFalse(self._may_read(attachment, self.somebody_else))

    def test_a_file_the_customer_attached_themselves_still_opens(self):
        """Those carry no `res_id`, so they can only ever be reached by the
        first branch. Widening the guard must not have narrowed this one."""
        attachment = self.env["ir.attachment"].with_user(self.accountant).create({
            "name": "invoice.pdf", "raw": b"x",
            "res_model": "razyyn.agent.chat.session",
        })
        self.assertFalse(attachment.res_id)
        self.assertTrue(self._may_read(attachment, self.accountant))
        self.assertFalse(self._may_read(attachment, self.somebody_else))

    def test_the_id_is_read_whether_it_arrives_as_a_number_or_as_text(self):
        """The path types it; the query does not.

        `<int:attachment_id>` hands this route a NUMBER. The same argument
        filled from the query string arrives as the TEXT "1549", because Odoo
        merges a query string into a call's arguments exactly as written. Given
        to `browse` unread, text is taken for a sequence of ids -- one per
        digit -- which matches nothing and answers "not found" with no sign of
        why. That is a working link that opens an empty 404.
        """
        session = self._conversation_of(self.accountant, "aaaaaaaa-0000-0000-0000-00000000000b")
        stored = svc.save_generated_file(
            self.env, session.session_id, "Statement.pdf", b"%PDF-1.4 fake",
            self._connection_of(self.accountant))

        for spelling in (stored["attachment_id"], str(stored["attachment_id"])):
            request = _Request(self.env(user=self.accountant))
            with patch.object(chat_client_api, "request", request):
                chat_client_api.RazyynChatApi().download_file(spelling)
            self.assertEqual(request.served[0], b"%PDF-1.4 fake",
                             f"the file did not come back for {spelling!r}")

    def test_an_id_that_is_not_a_number_is_refused(self):
        import werkzeug.exceptions

        request = _Request(self.env(user=self.accountant))
        with patch.object(chat_client_api, "request", request):
            with self.assertRaises(werkzeug.exceptions.NotFound):
                chat_client_api.RazyynChatApi().download_file("../../etc/passwd")
