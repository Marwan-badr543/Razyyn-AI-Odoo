# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""No account is created for somebody who did not agree to the terms.

WHY THE BROWSER IS NOT THE GATE
    The sign-up card refuses to submit with the box unticked, and that is the
    only refusal a customer ever sees. It is not the one that protects
    anything: `/razyyn/api/authenticate_agent` is reachable by anyone signed
    into this Odoo, with whatever body they like, and the field would otherwise
    fall into `**_kwargs` and be dropped without a word.

    So the refusal is here, in front of everything the sign-up does -- before a
    settings row is made, before a key is minted, before the platform is called
    at all. This test calls the method directly, which is exactly the shape of
    the call the browser cannot be trusted to make.

    odoo-bin -d <db> --test-enable --test-tags /razyyn_ai -u razyyn_ai
"""

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..controllers.chat_client_api import RazyynChatApi


@tagged("post_install", "-at_install")
class TestTheTermsAreAgreedTo(TransactionCase):

    def _sign_up(self, **overrides):
        fields = {
            "email": "terms@razyyn.test",
            "password": "a-long-password",
            "company_name": "Terms Co",
            "country_code": "EG",
        }
        fields.update(overrides)
        return RazyynChatApi()._sign_up(**fields)

    def test_an_unticked_box_is_refused(self):
        with self.assertRaises(UserError) as refusal:
            self._sign_up(accepted_terms=False)
        self.assertIn("Terms of Use", str(refusal.exception))

    def test_a_call_that_never_mentions_the_box_is_refused_too(self):
        """An older chat window -- or anything that is not one -- sends no such
        field. Absent is not agreement."""
        with self.assertRaises(UserError) as refusal:
            self._sign_up()
        self.assertIn("Terms of Use", str(refusal.exception))

    def test_the_refusal_leaves_no_settings_row_behind(self):
        """The sign-up mints a key and writes a row before it calls the
        platform. A refusal after that would leave the leftovers of an account
        that was never created, and the next honest attempt reads them as a
        duplicate."""
        settings = self.env["razyyn.agent.settings"].sudo()
        before = settings.search_count([])
        with self.assertRaises(UserError):
            self._sign_up(accepted_terms=False)
        self.assertEqual(settings.search_count([]), before)

    def test_the_words_a_person_typed_do_not_count_as_a_tick(self):
        """The field crosses a JSON-RPC boundary, so "false" arrives as a
        perfectly true string unless it is read through `_as_bool`."""
        for answer in (False, "false", "", "0", None, "no"):
            with self.assertRaises(UserError):
                self._sign_up(accepted_terms=answer)
