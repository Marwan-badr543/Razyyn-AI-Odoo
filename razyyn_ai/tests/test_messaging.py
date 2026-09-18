# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Sending a message: what this site offers, what it refuses, what it records.

NO PROVIDER IS CALLED HERE. Telegram's own HTTP is replaced, because what
these cases are about is this side of the wire — which destinations are
permitted, what a refusal says, and whether an attempt leaves a record. That
the real API works is proved against the real API by
``tools/create_desk_check.py``, which sends a real message to a real chat.
"""

import base64
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import Form, TransactionCase

from ..services import agent_messaging_service as messaging


class _Reply:
    """Enough of a ``requests`` response for the receipt readers."""

    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _telegram_accepts(message_id=4242):
    return _Reply({"ok": True, "result": {"message_id": message_id}})


#: Every box on the configuration, emptied. A case that names only the
#: settings it cares about inherits the rest from whatever the site happens to
#: have -- and the site these were first run against had nothing, so they
#: passed. Run against a database with a mailbox already set up, three of them
#: failed: the e-mail channel was available when the case had arranged for it
#: to be unavailable, and the real mailbox's username turned up where an empty
#: one was expected. The fixture states the whole configuration now.
A_BLANK_CONFIGURATION = {
    "email_enabled": False, "email_from": False, "email_sender_name": False,
    "email_last_error": False,
    "smtp_host": False, "smtp_port": 0, "smtp_security": False,
    "smtp_username": False, "smtp_password": False,
    "telegram_enabled": False, "telegram_bot_token": False,
    "telegram_last_error": False,
    "slack_enabled": False, "slack_bot_token": False, "slack_last_error": False,
    "destination_ids": [(5, 0, 0)],
}


class MessagingTestCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["razyyn.agent.messaging.settings"].sudo() \
            .get_settings_singleton()
        self.settings.write(dict(A_BLANK_CONFIGURATION, **{
            "telegram_enabled": True,
            "telegram_bot_token": "test-token-not-a-real-one",
            "slack_enabled": False,
            "email_enabled": True,
            "destination_ids": [(5, 0, 0), (0, 0, {
                "channel": "telegram", "label": "Finance team",
                "address": "-100123", "is_default": True,
            })],
        }))

    def _refused(self, **kwargs):
        """Call ``send_message`` expecting a refusal, and return it.

        NOT ``assertRaises``. Odoo's own ``assertRaises`` runs the block inside
        a savepoint and rolls back to it, so that a failed ORM call cannot
        poison the rest of the test. That is exactly the wrong tool here: what
        these cases are about is the row a refusal LEAVES BEHIND, and rolling
        back takes the row with it. Four of them failed for that reason while
        the code under test was doing precisely the right thing.
        """
        try:
            messaging.send_message(self.env, **kwargs)
        except messaging.MessagingError as exc:
            return exc
        self.fail("this should have been refused, and was not")

    def _log_for(self, key):
        return self.env["razyyn.agent.message.log"].sudo().search(
            [("idempotency_key", "=", key)], order="id")

    # ── What the agent is told ───────────────────────────────────────────────

    def test_the_reply_is_keyed_by_the_channel_names_the_agent_knows(self):
        """The client reads `channels['telegram']`. Any other shape, and it
        reports the site as having no message sending at all."""
        channels = messaging.get_messaging_config(self.env)["channels"]
        self.assertEqual(set(channels), {"gmail", "telegram", "slack"})

    def test_a_destination_is_offered_by_name_and_never_by_address(self):
        """The label is what an accountant says out loud. The chat id is the
        credential half — the agent is never given one, so it cannot invent
        another."""
        telegram = messaging.get_messaging_config(self.env)["channels"]["telegram"]
        self.assertEqual(
            telegram["destinations"],
            [{"label": "Finance team", "is_default": True, "notes": ""}],
        )
        self.assertNotIn("-100123", str(telegram))

    def test_no_token_is_ever_in_the_reply(self):
        self.assertNotIn(
            "test-token-not-a-real-one",
            str(messaging.get_messaging_config(self.env)),
        )

    def test_email_is_not_offered_when_there_is_nothing_to_send_it_through(self):
        """The switch is not the capability. It defaults to ON and every Odoo
        has a company e-mail address — on a fresh database, Odoo's own demo
        one. Read as readiness, those two had the agent announcing "I can email
        that, from info@yourcompany.com, to any address" on a site with no
        outgoing mail server at all."""
        # A sender, so that the mail server is unambiguously the only thing
        # missing. On a database set up without demo data the company has no
        # e-mail address either, and the gap this names would then be that one
        # — true, but not what this case is about.
        self.settings.email_from = "reports@example.com"
        self.env["ir.mail_server"].sudo().search([]).unlink()
        gmail = messaging.get_messaging_config(self.env)["channels"]["gmail"]
        self.assertFalse(gmail["enabled"])
        self.assertIn("outgoing mail server", gmail["unavailable_reason"])

        refusal = self._refused(channel="gmail", destination="someone@example.com",
                                body="x", idempotency_key="case-email")
        self.assertIsInstance(refusal, messaging.ChannelNotConfiguredError)

    def test_email_with_a_server_and_no_sender_is_not_offered_either(self):
        self.env["ir.mail_server"].sudo().create({
            "name": "case", "smtp_host": "localhost",
        })
        self.env.company.email = False
        self.settings.email_from = False
        gmail = messaging.get_messaging_config(self.env)["channels"]["gmail"]
        self.assertFalse(gmail["enabled"])
        self.assertIn("sending address", gmail["unavailable_reason"])

    def test_email_is_offered_once_all_three_are_in_place(self):
        self.env["ir.mail_server"].sudo().create({
            "name": "case", "smtp_host": "localhost",
        })
        self.settings.email_from = "reports@example.com"
        gmail = messaging.get_messaging_config(self.env)["channels"]["gmail"]
        self.assertTrue(gmail["enabled"])
        self.assertEqual(gmail["sender"], "reports@example.com")
        self.assertTrue(gmail["accepts_any_address"])

    def test_an_email_really_goes_through_this_systems_mail_server(self):
        """The only thing replaced is the socket. Everything above it — the
        mail record, the sender formatting, the attachment, Odoo's own sending
        machinery — runs, because that is where this path can be wrong.

        It is also the channel that is switched ON by default, so it is the one
        a customer meets first.
        """
        self.env["ir.mail_server"].sudo().create({
            "name": "case", "smtp_host": "localhost",
        })
        self.settings.email_from = "reports@example.com"
        self.settings.email_sender_name = "Razyyn Accounts"
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "march-vat.pdf",
            "datas": base64.b64encode(b"not really a pdf"),
        })
        before = self.env["ir.attachment"].sudo().search_count([])

        with patch("odoo.addons.base.models.ir_mail_server.IrMailServer.send_email",
                   return_value="<case@localhost>") as sent:
            receipt = messaging.send_message(
                self.env, channel="gmail", destination="someone@example.com",
                subject="March VAT", body="Attached.",
                file_urls=[f"/razyyn/chat/download_file?attachment_id={attachment.id}"],
                idempotency_key="case-email-send",
            )

        self.assertEqual(receipt["status"], "SENT")
        self.assertTrue(receipt["provider_message_id"])
        self.assertEqual(sent.call_count, 1)

        message = sent.call_args.args[0]
        self.assertEqual(message["To"], "someone@example.com")
        self.assertIn("reports@example.com", message["From"])
        self.assertEqual(self._log_for("case-email-send").status, "sent")

        # THE COPIES THE SEND MADE MUST NOT OUTLIVE IT. They are second copies
        # of a file this site already holds, and they sit outside the hourly
        # sweep that clears generated reports — left behind, every emailed
        # report is kept twice for ever.
        self.assertEqual(self.env["ir.attachment"].sudo().search_count([]), before)
        self.assertTrue(attachment.exists())

    # ── the practice's own mailbox ───────────────────────────────────────
    #
    # The path that lets a small practice send at all. A Google service account
    # needed a Workspace ADMINISTRATOR to authorise it, and Odoo's own outgoing
    # mail server needs somebody to have configured one — a two-person firm on
    # a free Gmail address has neither, and could not email an invoice to a
    # client. These cases are about the mailbox they sign in to themselves.

    def _own_mailbox(self):
        self.settings.sudo().write({
            "email_from": "reports@example.com",
            "smtp_host": "smtp.gmail.com",
            "smtp_security": "starttls",
            "smtp_password": "abcd efgh ijkl mnop",
        })

    def test_a_mailbox_of_their_own_is_enough_with_no_odoo_mail_server(self):
        """This is the whole point of the change: no ir.mail_server anywhere."""
        self.env["ir.mail_server"].sudo().search([]).unlink()
        self._own_mailbox()
        gmail = messaging.get_messaging_config(self.env)["channels"]["gmail"]
        self.assertTrue(gmail["enabled"])
        self.assertEqual(gmail["sender"], "reports@example.com")

    def test_their_own_mailbox_carries_the_message_and_names_it(self):
        """The credentials reach the connection, and a reference comes back.

        SMTP returns NOTHING on success — no id, no receipt. The id is written
        into the header before the message leaves, so the customer is given a
        reference that is actually in the recipient's copy of the email.
        """
        self.env["ir.mail_server"].sudo().search([]).unlink()
        self._own_mailbox()
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "march-vat.pdf",
            "datas": base64.b64encode(b"not really a pdf"),
        })

        # build_email is NOT mocked: it is the one that unpacks the attachment
        # tuples, and its own docstring disagrees with its code about how many
        # there are. A pair raises on the unpack, so every message carrying a
        # file would fail while every message without one worked.
        with patch("odoo.addons.base.models.ir_mail_server.IrMailServer.connect") as connect, \
             patch("odoo.addons.base.models.ir_mail_server.IrMailServer.send_email") as sent:
            receipt = messaging.send_message(
                self.env, channel="gmail", destination="someone@example.com",
                subject="March VAT", body="Attached.",
                file_urls=[f"/razyyn/chat/download_file?attachment_id={attachment.id}"],
                idempotency_key="case-own-smtp",
            )

        self.assertEqual(receipt["status"], "SENT")
        self.assertTrue(receipt["provider_message_id"].startswith("<"))
        self.assertEqual(connect.call_args.kwargs["host"], "smtp.gmail.com")
        self.assertEqual(connect.call_args.kwargs["port"], 587)
        self.assertEqual(connect.call_args.kwargs["encryption"], "starttls")
        # The username was left empty, which must mean the Send As address
        # rather than an empty login the server would refuse.
        self.assertEqual(connect.call_args.kwargs["user"], "reports@example.com")

        message = sent.call_args.args[0]
        self.assertEqual(message["Message-ID"], receipt["provider_message_id"])
        self.assertEqual(message["To"], "someone@example.com")
        self.assertIn("reports@example.com", message["From"])
        attached = [part.get_filename() for part in message.walk()
                    if part.get_filename()]
        self.assertEqual(attached, ["march-vat.pdf"])
        self.assertEqual(self._log_for("case-own-smtp").status, "sent")

    def test_a_refused_sign_in_names_the_app_password(self):
        """The one failure every Gmail customer hits.

        Google refuses an account password outright and says only "Username and
        Password not accepted", which reads as a typo and sends them to retype
        the very password that can never work.
        """
        self.env["ir.mail_server"].sudo().search([]).unlink()
        self._own_mailbox()
        with patch("odoo.addons.base.models.ir_mail_server.IrMailServer.connect",
                   side_effect=Exception(
                       "535 5.7.8 Username and Password not accepted")):
            raised = self._refused(
                channel="gmail", destination="someone@example.com", body="x",
                idempotency_key="case-own-smtp-auth",
            )
        self.assertIsInstance(raised, messaging.ProviderRefusedError)
        self.assertEqual(raised.code, "SMTP_AUTH_REFUSED")
        self.assertIn("App Password", raised.detail)
        self.assertEqual(self._log_for("case-own-smtp-auth").status, "failed")

    def test_an_email_the_mail_server_refuses_is_not_reported_as_sent(self):
        self.env["ir.mail_server"].sudo().create({
            "name": "case", "smtp_host": "localhost",
        })
        self.settings.email_from = "reports@example.com"
        with patch("odoo.addons.base.models.ir_mail_server.IrMailServer.send_email",
                   side_effect=Exception("mailbox unavailable")):
            raised = self._refused(channel="gmail", destination="someone@example.com",
                                   body="x", idempotency_key="case-email-fail")
        self.assertIsInstance(raised, messaging.ProviderRefusedError)
        self.assertEqual(self._log_for("case-email-fail").status, "failed")

    def test_an_address_that_is_not_an_address_is_refused(self):
        self.env["ir.mail_server"].sudo().create({
            "name": "case", "smtp_host": "localhost",
        })
        self.settings.email_from = "reports@example.com"
        raised = self._refused(channel="gmail", destination="the auditors",
                               body="x", idempotency_key="case-email-address")
        self.assertIsInstance(raised, messaging.UnknownDestinationError)

    def test_a_channel_that_cannot_be_used_says_why(self):
        """'Slack is off' and 'Slack has no destinations' send an administrator
        to two different screens, so the reason is the site's, not a fixed
        string the agent made up."""
        slack = messaging.get_messaging_config(self.env)["channels"]["slack"]
        self.assertFalse(slack["enabled"])
        self.assertIn("switched off", slack["unavailable_reason"])

        self.settings.slack_enabled = True
        slack = messaging.get_messaging_config(self.env)["channels"]["slack"]
        self.assertIn("token", slack["unavailable_reason"])

        self.settings.slack_bot_token = "xoxb-test"
        slack = messaging.get_messaging_config(self.env)["channels"]["slack"]
        self.assertIn("destinations", slack["unavailable_reason"])

    def test_telegram_with_a_token_and_no_destination_is_not_usable(self):
        """A bot cannot start a conversation, so a token alone can reach
        nobody — and reporting it as ready is how the agent comes to offer
        something that then fails."""
        self.settings.destination_ids.unlink()
        telegram = messaging.get_messaging_config(self.env)["channels"]["telegram"]
        self.assertFalse(telegram["enabled"])

    # ── Sending ──────────────────────────────────────────────────────────────

    def test_a_send_answers_with_the_providers_own_message_id(self):
        """The agent may say a message was sent only when there is an id
        backing it. 'Nothing raised' is not evidence of delivery."""
        with patch.object(messaging.requests, "post", return_value=_telegram_accepts()):
            receipt = messaging.send_message(
                self.env, channel="telegram", body="hello",
                idempotency_key="case-1",
            )
        self.assertEqual(receipt["status"], "SENT")
        self.assertEqual(receipt["provider_message_id"], "4242")
        self.assertEqual(receipt["destination_label"], "Finance team")

    def test_the_subject_becomes_the_first_line(self):
        """Telegram has no subject, and dropping it loses the one line naming
        what a report is."""
        with patch.object(messaging.requests, "post",
                          return_value=_telegram_accepts()) as sent:
            messaging.send_message(self.env, channel="telegram",
                                   subject="March VAT", body="Attached.",
                                   idempotency_key="case-2")
        self.assertEqual(sent.call_args.kwargs["json"]["text"], "March VAT\n\nAttached.")

    def test_the_same_instruction_twice_sends_once(self):
        """A retry that emails a client's auditor a second copy of their trial
        balance is not a small mistake."""
        with patch.object(messaging.requests, "post",
                          return_value=_telegram_accepts()) as sent:
            first = messaging.send_message(self.env, channel="telegram",
                                           body="one", idempotency_key="case-3")
            second = messaging.send_message(self.env, channel="telegram",
                                            body="one", idempotency_key="case-3")
        self.assertEqual(sent.call_count, 1)
        self.assertTrue(second.get("replayed"))
        self.assertEqual(second["provider_message_id"], first["provider_message_id"])

    def test_a_refused_attempt_can_be_retried_under_the_same_key(self):
        """Only a SENT attempt blocks a repeat. The usual reason for a refusal
        is a setting an administrator then goes and fixes, and the accountant's
        next words are 'try again'."""
        self.settings.telegram_enabled = False
        refusal = self._refused(channel="telegram", body="x",
                                idempotency_key="case-4")
        self.assertIsInstance(refusal, messaging.ChannelNotConfiguredError)

        self.settings.telegram_enabled = True
        with patch.object(messaging.requests, "post", return_value=_telegram_accepts()):
            receipt = messaging.send_message(self.env, channel="telegram", body="x",
                                             idempotency_key="case-4")
        self.assertEqual(receipt["status"], "SENT")
        self.assertEqual(self._log_for("case-4").mapped("status"),
                         ["refused", "sent"])

    def test_an_unlisted_destination_is_refused_and_names_what_exists(self):
        refusal = self._refused(channel="telegram", destination="the auditors",
                                body="x", idempotency_key="case-5")
        self.assertIsInstance(refusal, messaging.UnknownDestinationError)
        self.assertIn("Finance team", refusal.detail)

    def test_two_destinations_and_no_default_asks_rather_than_guessing(self):
        self.settings.destination_ids.is_default = False
        self.settings.write({"destination_ids": [(0, 0, {
            "channel": "telegram", "label": "Partners", "address": "-100999",
        })]})
        refusal = self._refused(channel="telegram", body="x",
                                idempotency_key="case-6")
        self.assertIsInstance(refusal, messaging.UnknownDestinationError)
        self.assertIn("Partners", refusal.detail)

    def test_a_provider_refusal_is_not_reported_as_a_send(self):
        refusal = _Reply({"ok": False, "description": "chat not found"})
        with patch.object(messaging.requests, "post", return_value=refusal):
            raised = self._refused(channel="telegram", body="x",
                                   idempotency_key="case-7")
        self.assertIsInstance(raised, messaging.ProviderRefusedError)
        self.assertIn("chat not found", raised.detail)
        self.assertEqual(self._log_for("case-7").mapped("status"), ["failed"])

    def test_an_acceptance_with_no_message_id_is_not_a_receipt(self):
        """Accepted and unconfirmable. Without an id there is nothing to show
        the customer, so it must not be reported as sent."""
        with patch.object(messaging.requests, "post",
                          return_value=_Reply({"ok": True, "result": {}})):
            raised = self._refused(channel="telegram", body="x",
                                   idempotency_key="case-8")
        self.assertIsInstance(raised, messaging.ProviderRefusedError)

    # ── Attachments ──────────────────────────────────────────────────────────

    def test_a_file_this_site_holds_is_sent_and_named_in_the_log(self):
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "march-vat.pdf",
            "datas": base64.b64encode(b"not really a pdf"),
        })
        with patch.object(messaging.requests, "post", return_value=_telegram_accepts()):
            messaging.send_message(
                self.env, channel="telegram", body="here it is",
                file_urls=[f"/razyyn/chat/download_file?attachment_id={attachment.id}"],
                idempotency_key="case-9",
            )
        self.assertEqual(self._log_for("case-9").attachment_names, "march-vat.pdf")

    def test_a_path_on_disk_is_not_a_file_this_site_holds(self):
        """Accepting a path would make this an arbitrary file read on the
        customer's server, reachable by anything that can reach the agent."""
        for address in ("/etc/passwd", "../../etc/passwd", "march-vat.pdf"):
            with self.subTest(address=address):
                self.assertIsInstance(
                    self._refused(channel="telegram", body="x",
                                  file_urls=[address],
                                  idempotency_key=f"case-10-{address}"),
                    messaging.AttachmentError,
                )

    # ── Refusals that never reach a provider ─────────────────────────────────

    def test_a_channel_this_system_does_not_have_is_refused(self):
        self.assertEqual(
            self._refused(channel="carrier pigeon", idempotency_key="case-11").code,
            "UNKNOWN_CHANNEL")

    def test_a_send_without_an_idempotency_key_is_refused(self):
        self.assertEqual(
            self._refused(channel="telegram", body="x", idempotency_key="").code,
            "MISSING_IDEMPOTENCY_KEY")

    def test_the_callers_api_key_is_not_mistaken_for_part_of_the_message(self):
        """The route hands this the request's own parameters, key and all."""
        with patch.object(messaging.requests, "post", return_value=_telegram_accepts()):
            receipt = messaging.send_message(
                self.env, channel="telegram", body="x", idempotency_key="case-12",
                api_key="the-callers-key", something_new="ignored",
            )
        self.assertEqual(receipt["status"], "SENT")
        self.assertNotIn("the-callers-key", str(self._log_for("case-12").read()))

    # ── The record ───────────────────────────────────────────────────────────

    def test_every_attempt_leaves_a_row_whatever_happened_to_it(self):
        """An outbound record holding only successes cannot answer 'did the
        agent send that?', which is the question an auditor asks."""
        self._refused(channel="telegram", destination="nobody", body="x",
                      idempotency_key="case-13")
        row = self._log_for("case-13")
        self.assertEqual(row.status, "refused")
        self.assertEqual(row.error_code, "UNKNOWN_DESTINATION")
        self.assertTrue(row.error_message)

    def test_a_failure_is_left_where_an_administrator_will_see_it(self):
        """The person who can fix a bad token is looking at the settings form,
        not at the accountant's chat window."""
        with patch.object(messaging.requests, "post",
                          return_value=_Reply({"ok": False, "description": "Unauthorized"})):
            self._refused(channel="telegram", body="x", idempotency_key="case-14")
        self.assertIn("Unauthorized", self.settings.telegram_last_error)

        with patch.object(messaging.requests, "post", return_value=_telegram_accepts()):
            messaging.send_message(self.env, channel="telegram", body="x",
                                   idempotency_key="case-15")
        self.assertFalse(self.settings.telegram_last_error)


class MessagingConfigurationShapeTestCase(TransactionCase):
    """One configuration record, and destinations that stay on their own tab.

    Both of these were bugs whose symptom was silence. A second configuration
    record could be filled in for ever and never read, so an administrator who
    saved a mailbox password into it went on being told email was not set up.
    And a destination row that took its channel from a default rather than from
    the tab it was typed in vanished from that tab and left the channel
    reporting that it had nowhere to send.
    """

    def setUp(self):
        super().setUp()
        self.settings = self.env["razyyn.agent.messaging.settings"].sudo() \
            .get_settings_singleton()

    def test_there_can_only_ever_be_one_configuration(self):
        with self.assertRaises(UserError):
            self.env["razyyn.agent.messaging.settings"].sudo().create({
                "name": "A second one",
            })

    def test_the_menu_opens_the_record_that_is_actually_read(self):
        """Not a list — a list carries a New button, and the record that button
        makes is the one nothing reads."""
        action = self.env["razyyn.agent.messaging.settings"].sudo() \
            .action_open_settings()
        self.assertEqual(action["res_id"], self.settings.id)
        self.assertEqual(action["view_mode"], "form")

    def test_a_row_typed_on_a_tab_belongs_to_that_tab(self):
        """The context each tab carries is what files the row, and this is the
        case that proves it rather than the form's own markup."""
        for field, channel in (
            ("email_destination_ids", "gmail"),
            ("telegram_destination_ids", "telegram"),
            ("slack_destination_ids", "slack"),
        ):
            with self.subTest(field=field):
                self.settings.write({field: [(0, 0, {
                    "label": f"{channel} row", "address": f"{channel}-address",
                })]})
                row = self.settings[field].filtered(
                    lambda r: r.label == f"{channel} row")
                self.assertEqual(row.channel, channel)

    def test_a_destination_shows_only_on_its_own_tab(self):
        self.settings.write({"destination_ids": [(5, 0, 0)]})
        self.settings.write({"telegram_destination_ids": [(0, 0, {
            "label": "Finance team", "address": "-100123",
        })]})
        self.assertEqual(self.settings.telegram_destination_ids.mapped("label"),
                         ["Finance team"])
        self.assertFalse(self.settings.slack_destination_ids)
        self.assertFalse(self.settings.email_destination_ids)

    def test_a_row_with_no_channel_is_refused_rather_than_misfiled(self):
        """It used to default to Telegram, which is how a Slack destination
        ended up somewhere nobody would look for it."""
        with self.assertRaises(Exception):
            self.env["razyyn.agent.messaging.destination"].sudo().create({
                "settings_id": self.settings.id,
                "label": "Nowhere", "address": "x",
            })


class EmailAddressBookTestCase(TransactionCase):
    """Saving who you email, so nobody types the address twice.

    The list here is NOT the permitted set it is on Telegram and Slack. Email
    reaches anybody, so saving a name only saves typing: an address given in
    full must go on working exactly as it did.
    """

    def setUp(self):
        super().setUp()
        self.settings = self.env["razyyn.agent.messaging.settings"].sudo() \
            .get_settings_singleton()
        self.settings.write({
            "email_enabled": True,
            "email_from": "reports@example.com",
            "destination_ids": [(5, 0, 0)],
            "email_destination_ids": [
                (0, 0, {"label": "Marwan", "address": "marwan@example.com",
                        "is_default": True}),
                (0, 0, {"label": "The auditors", "address": "audit@example.com"}),
            ],
        })
        self.env["ir.mail_server"].sudo().create({
            "name": "case", "smtp_host": "localhost",
        })

    def _sent_to(self, **kwargs):
        with patch("odoo.addons.base.models.ir_mail_server.IrMailServer.send_email",
                   return_value="<case@localhost>") as sent:
            receipt = messaging.send_message(self.env, channel="gmail", **kwargs)
        return receipt, sent.call_args.args[0]["To"]

    def test_the_agent_is_told_the_names(self):
        """Storing the address book does nothing unless the agent learns the
        names exist — 'email it to Marwan' can only work if something said who
        Marwan is."""
        gmail = messaging.get_messaging_config(self.env)["channels"]["gmail"]
        self.assertEqual([d["label"] for d in gmail["destinations"]],
                         ["Marwan", "The auditors"])
        self.assertTrue(gmail["accepts_any_address"])

    def test_an_email_address_travels_and_a_chat_id_does_not(self):
        """The difference is what withholding it buys. A Telegram chat id kept
        back is a chat id the agent cannot invent. Email reaches any address
        the agent types anyway, so keeping a saved one back buys nothing — and
        costs the customer an approval card naming a recipient they cannot
        check."""
        self.settings.write({"telegram_enabled": True,
                             "telegram_bot_token": "not-a-real-token"})
        self.settings.write({"telegram_destination_ids": [(0, 0, {
            "label": "Finance team", "address": "-100123",
        })]})
        channels = messaging.get_messaging_config(self.env)["channels"]
        self.assertIn("marwan@example.com",
                      [d["address"] for d in channels["gmail"]["destinations"]])
        self.assertNotIn("-100123", str(channels["telegram"]))

    def test_a_name_is_sent_to_the_address_saved_under_it(self):
        receipt, to = self._sent_to(destination="Marwan", body="x",
                                    idempotency_key="book-1")
        self.assertEqual(to, "marwan@example.com")
        self.assertEqual(receipt["destination_label"], "Marwan")

    def test_the_name_is_read_however_it_was_typed(self):
        _, to = self._sent_to(destination="  the AUDITORS ", body="x",
                              idempotency_key="book-2")
        self.assertEqual(to, "audit@example.com")

    def test_an_address_given_in_full_still_goes_exactly_there(self):
        """The address book must not become a fence. Email reaches anybody."""
        _, to = self._sent_to(destination="someone-new@example.com", body="x",
                              idempotency_key="book-3")
        self.assertEqual(to, "someone-new@example.com")

    def test_a_saved_address_is_named_in_the_receipt_by_its_saved_name(self):
        receipt, _ = self._sent_to(destination="marwan@example.com", body="x",
                                   idempotency_key="book-4")
        self.assertEqual(receipt["destination_label"], "Marwan")

    def test_naming_nobody_uses_the_default_recipient(self):
        receipt, to = self._sent_to(body="x", idempotency_key="book-5")
        self.assertEqual(to, "marwan@example.com")
        self.assertEqual(receipt["destination_label"], "Marwan")

    def test_a_name_nobody_saved_is_refused_and_says_what_is_saved(self):
        """And says the way out: the address itself always works."""
        try:
            messaging.send_message(self.env, channel="gmail",
                                   destination="the tax office", body="x",
                                   idempotency_key="book-6")
        except messaging.UnknownDestinationError as exc:
            self.assertIn("Marwan", exc.detail)
            self.assertIn("The auditors", exc.detail)
            self.assertIn("address", exc.detail)
        else:
            self.fail("an unsaved name should have been refused")

    def test_email_is_usable_with_nobody_saved_at_all(self):
        """The saved list is a convenience. Requiring one would turn a helpful
        addition into a channel that stopped working."""
        self.settings.email_destination_ids.unlink()
        gmail = messaging.get_messaging_config(self.env)["channels"]["gmail"]
        self.assertTrue(gmail["enabled"])
        self.assertEqual(gmail["destinations"], [])
        _, to = self._sent_to(destination="someone@example.com", body="x",
                              idempotency_key="book-7")
        self.assertEqual(to, "someone@example.com")


class OneDefaultPerChannelTestCase(TransactionCase):
    """Two defaults is not a form error anybody notices. It is a report sent to
    the wrong colleague months later, because the send path takes the first
    default it finds and nothing said which one was meant."""

    def setUp(self):
        super().setUp()
        self.settings = self.env["razyyn.agent.messaging.settings"].sudo() \
            .get_settings_singleton()
        self.settings.destination_ids.unlink()

    def _add(self, channel, label, is_default=False):
        return self.env["razyyn.agent.messaging.destination"].sudo().create({
            "settings_id": self.settings.id, "channel": channel,
            "label": label, "address": f"{label}-address",
            "is_default": is_default,
        })

    def test_the_newest_choice_wins(self):
        """Ticking a new default is how somebody says "this one now"."""
        old = self._add("gmail", "Old", is_default=True)
        new = self._add("gmail", "New", is_default=True)
        self.assertFalse(old.is_default)
        self.assertTrue(new.is_default)

    def test_it_is_settled_on_a_later_edit_too(self):
        first = self._add("telegram", "First", is_default=True)
        second = self._add("telegram", "Second")
        second.is_default = True
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)

    def test_each_channel_keeps_its_own_default(self):
        """Clearing across channels would leave a channel with none at all."""
        email = self._add("gmail", "Marwan", is_default=True)
        telegram = self._add("telegram", "Finance team", is_default=True)
        self.assertTrue(email.is_default)
        self.assertTrue(telegram.is_default)

    def test_two_ticked_in_one_write_leave_one_standing(self):
        """They used to clear EACH OTHER. The channel was then left with no
        default at all and the agent asked which to use, over a list where
        somebody had plainly chosen."""
        self.settings.write({"email_destination_ids": [
            (0, 0, {"label": "First", "address": "first@example.com",
                    "is_default": True}),
            (0, 0, {"label": "Second", "address": "second@example.com",
                    "is_default": True}),
        ]})
        defaults = self.settings.email_destination_ids.filtered("is_default")
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults.label, "Second")

    def test_the_form_corrects_it_as_it_is_typed(self):
        """Driven through the form rather than through a write, and that is the
        point of it.

        The form is a different road into the same table and it does not obey
        the same signposts: the channel each tab files its rows under is set by
        a context, and the one declared on the model is applied to an ORM write
        while the one the client reads has to be on the view. With only the
        first of them, every row typed on a tab arrived with no channel at all
        and was refused on save — and nothing but a case driven through the
        form would have said so."""
        with Form(self.settings) as form:
            # The list only appears once the channel is switched on — the same
            # gating the Frappe form has, so an administrator sets a channel up
            # in one place rather than two.
            form.telegram_enabled = True
            with form.telegram_destination_ids.new() as line:
                line.label = "First"
                line.address = "-1"
                line.is_default = True
            with form.telegram_destination_ids.new() as line:
                line.label = "Second"
                line.address = "-2"
                line.is_default = True

        defaults = self.settings.telegram_destination_ids.filtered("is_default")
        self.assertEqual(defaults.label, "Second")

    def test_nobody_has_to_nominate_one(self):
        """Naming the destination every time is an ordinary way to work."""
        rows = self._add("slack", "A") | self._add("slack", "B")
        self.assertFalse(any(rows.mapped("is_default")))



def _the_fold():
    """The 1.7.0 migration, loaded by path — migrations/ is not a package."""
    import importlib.util
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "migrations", "1.7.0", "post-migration.py")
    spec = importlib.util.spec_from_file_location("razyyn_fold_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.migrate


class FoldingOldConfigurationsTestCase(TransactionCase):
    """Sites that already made a second configuration must not lose it.

    Only the first record was ever read, so everything typed into the others
    did nothing — and the symptom was the agent going on saying email was not
    set up. Refusing a second one from now on fixes nothing for those sites by
    itself: what they typed has to be carried across, or the fix reads to them
    as their configuration disappearing.
    """

    def setUp(self):
        super().setUp()
        self.model = self.env["razyyn.agent.messaging.settings"].sudo()
        self.keeper = self.model.get_settings_singleton()
        # Emptied for the same reason as above: what the fold carries across is
        # decided by which boxes are already full, so a site's own mailbox
        # would answer these cases instead of the fixture.
        self.keeper.write(A_BLANK_CONFIGURATION)

    def _a_second_record(self, **values):
        """Made the way those sites made theirs — before the guard existed."""
        columns = ["name"] + list(values)
        self.env.cr.execute(
            # now() AT TIME ZONE 'UTC', never a bare now(): this database
            # answers in Africa/Cairo and Odoo stores UTC, so a bare now()
            # writes a row three hours in the future of every date the ORM
            # wrote. Harmless for these cases, which only compare the extras
            # with each other -- and a trap for the next one that does not.
            "INSERT INTO razyyn_agent_messaging_settings ({}, create_uid, "
            "write_uid, create_date, write_date) VALUES ({}, 1, 1, "
            "(now() AT TIME ZONE 'UTC'), (now() AT TIME ZONE 'UTC')) "
            "RETURNING id".format(
                ", ".join(columns), ", ".join(["%s"] * len(columns)),
            ),
            ["A second one"] + list(values.values()),
        )
        return self.env.cr.fetchone()[0]

    def _fold(self):
        # THE FLUSH IS THE TEST, NOT HOUSEKEEPING. The migration reads the
        # table in SQL, and what this case has just written is still sitting in
        # the ORM waiting to go. Without this the fold reads the row as it was
        # BEFORE the setup -- so on a database that already had a
        # configuration it would see a box as full when the test had just
        # emptied it, carry nothing across, and then flush the pending write
        # over the top of whatever it did do. It passed on an empty database
        # and failed on a real one, which is the worst way for a test to be
        # wrong.
        self.env.flush_all()
        _the_fold()(self.env.cr, "17.0.1.6.0")
        self.env.invalidate_all()

    def test_what_was_typed_into_the_ignored_record_is_carried_across(self):
        self.keeper.write({"email_from": False, "telegram_bot_token": False})
        self._a_second_record(
            email_from="reports@example.com",
            telegram_bot_token="a-token-nobody-was-reading",
        )
        self._fold()
        self.assertEqual(self.keeper.email_from, "reports@example.com")
        self.assertEqual(self.keeper.telegram_bot_token,
                         "a-token-nobody-was-reading")

    def test_what_the_agent_was_already_using_is_not_overwritten(self):
        """The first record is the one that has been working. A merge that
        replaced it would change how a live site sends, which nobody asked for.
        """
        self.keeper.email_from = "in-use@example.com"
        self._a_second_record(email_from="never-read@example.com")
        self._fold()
        self.assertEqual(self.keeper.email_from, "in-use@example.com")

    def test_destinations_come_across_rather_than_being_deleted(self):
        """Each one is a place the company meant the agent to be able to
        reach, so none of them is a judgement call."""
        other = self._a_second_record()
        self.env.cr.execute(
            "INSERT INTO razyyn_agent_messaging_destination "
            "(settings_id, channel, label, address, sequence, create_uid, "
            " write_uid, create_date, write_date) "
            "VALUES (%s, 'telegram', 'Finance team', '-100123', 10, 1, 1, "
            "        (now() AT TIME ZONE 'UTC'), (now() AT TIME ZONE 'UTC'))",
            (other,),
        )
        self._fold()
        self.assertEqual(self.keeper.telegram_destination_ids.mapped("label"),
                         ["Finance team"])

    def test_afterwards_there_is_exactly_one(self):
        self._a_second_record()
        self._a_second_record()
        self._fold()
        self.assertEqual(self.model.search_count([]), 1)

    def test_running_it_again_changes_nothing(self):
        self._a_second_record(email_from="reports@example.com")
        self._fold()
        self._fold()
        self.assertEqual(self.model.search_count([]), 1)
        self.assertEqual(self.keeper.email_from, "reports@example.com")
