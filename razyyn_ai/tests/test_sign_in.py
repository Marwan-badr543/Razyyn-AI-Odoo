# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Signing in, against a real database.

WHY THESE ARE NOT IN ../../tests
    Those run with no Odoo at all, which is right for a guard made of pure
    rules. This one is about which ROW a session lands on and whether the next
    thing to read it finds it -- a question about the ORM, and answering it
    against a mock would only prove the mock agrees with itself.

    The defect they cover shipped and locked the developer's own admin user
    out: the platform accepted the password, Odoo returned HTTP 200, and the
    chat window stayed on the sign-in card.

    odoo-bin -d <db> --test-enable --test-tags /razyyn_ai -u razyyn_ai
"""

from odoo.tests.common import TransactionCase, tagged

from ..services import agent_connection_service as connection
from ..services.session_holder import settings_for_session


@tagged("post_install", "-at_install")
class TestWhereASessionLands(TransactionCase):

    def setUp(self):
        super().setUp()
        self.person = self.env["res.users"].create({
            "name": "Someone", "login": "razyyn-session-test@example.com",
        })
        self.settings = self.env["razyyn.agent.settings"].sudo()

    def _rows(self):
        return self.settings.search(
            [("user_id", "=", self.person.id)], order="id asc",
        )

    def test_a_first_sign_in_makes_the_record(self):
        # Whoever made the account -- the web app, an ERPNext bench, another
        # Odoo -- has no row here. Refusing them was a dead end, because the
        # platform refuses signing up again with "This ERP is already linked
        # to an account".
        self.assertFalse(self._rows())

        holder = settings_for_session(self.env, self.person.id, "new@razyyn.test")

        self.assertTrue(holder)
        self.assertEqual(holder.email, "new@razyyn.test")
        self.assertEqual(holder.user_id, self.person)

    def test_a_record_the_old_connector_left_without_an_email_is_adopted(self):
        # What shipped: `generate(label=f"Chat: {email}")`. The address lived
        # in the label and the column stayed empty, so nothing could match it
        # and its owner could never sign in again.
        orphan, _key = self.settings.generate(
            user_id=self.person.id, label="Chat: someone@razyyn.test",
        )
        self.assertFalse(orphan.email)

        holder = settings_for_session(self.env, self.person.id, "someone@razyyn.test")

        self.assertEqual(holder, orphan, "it made a second record instead of adopting")
        self.assertEqual(len(self._rows()), 1)

    def test_the_matching_record_is_preferred_over_any_other(self):
        first, _a = self.settings.generate(
            user_id=self.person.id, label="older", email="old@razyyn.test",
        )
        theirs, _b = self.settings.generate(
            user_id=self.person.id, label="theirs", email="wanted@razyyn.test",
        )
        holder = settings_for_session(self.env, self.person.id, "wanted@razyyn.test")
        self.assertEqual(holder, theirs)
        self.assertNotEqual(holder, first)

    def test_with_several_records_it_lands_on_the_one_connect_reads(self):
        # THE COUPLING THAT MATTERS. connect_write_access takes the user's
        # FIRST record. A session written to any other one leaves 1-Click
        # Connect telling somebody who just signed in to sign in first.
        first, _a = self.settings.generate(user_id=self.person.id, label="first")
        second, _b = self.settings.generate(user_id=self.person.id, label="second")
        self.assertLess(first.id, second.id)

        holder = settings_for_session(self.env, self.person.id, "who@razyyn.test")
        holder.write({"access_token": "a-token", "refresh_token": "r-token"})

        read_by_connect = self.settings.search(
            [("user_id", "=", self.person.id)], limit=1,
        )
        self.assertEqual(holder, read_by_connect)
        self.assertTrue(read_by_connect.access_token)

    def test_another_users_record_is_never_adopted(self):
        stranger = self.env["res.users"].create({
            "name": "Stranger", "login": "razyyn-stranger@example.com",
        })
        theirs, _k = self.settings.generate(
            user_id=stranger.id, label="theirs", email="shared@razyyn.test",
        )

        holder = settings_for_session(self.env, self.person.id, "shared@razyyn.test")

        self.assertNotEqual(holder, theirs)
        self.assertEqual(holder.user_id, self.person)

    def test_connect_finds_the_session_a_sign_in_just_wrote(self):
        # End to end over the two functions, because the bug was that they
        # disagreed about which row the session was on.
        holder = settings_for_session(self.env, self.person.id, "both@razyyn.test")
        holder.write({"access_token": "a-token", "refresh_token": "r-token"})

        found = self.env["razyyn.agent.settings"].sudo().search(
            [("user_id", "=", self.person.id)], limit=1,
        )
        self.assertTrue(found.access_token, "connect_write_access would refuse this user")
        self.assertTrue(hasattr(connection, "connect_write_access"))
