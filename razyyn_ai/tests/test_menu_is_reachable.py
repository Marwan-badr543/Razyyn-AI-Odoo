# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""What the menu actually shows, to the people who actually open it.

WHY THIS IS NOT ../../tests/test_menu_covers_every_model.py
    That one reads the XML and proves a menuitem exists. It cannot fail for
    the defect this one covers, because the menuitem DID exist: all eight were
    in views/menus.xml the whole time. Six of them were simply never rendered
    -- their heading carried `groups="razyyn_ai.group_razyyn_manager"`, the
    administrator was not in that group, and Odoo hides a menu whose parent is
    hidden. Counting `<menuitem>` elements would have reported a healthy menu
    while the administrator was looking at two entries.

    So this asks the web client's own question, `load_menus`, as a particular
    person, which is the only way to see a `groups` attribute and an
    ir.model.access row take effect.

    odoo-bin -d <db> --test-enable --test-tags /razyyn_ai -u razyyn_ai
"""

from odoo.tests.common import TransactionCase, tagged

# The eight models this module defines, by the XML id of the action that opens
# each. Keep in step with views/menus.xml; ../../tests/ fails if a NINTH model
# arrives with no menu at all, and this fails if one arrives that the
# administrator cannot see.
_EVERY_SCREEN = (
    "razyyn_ai.action_razyyn_chat_session",
    "razyyn_ai.action_razyyn_chat_message",
    "razyyn_ai.action_razyyn_chat_event",
    "razyyn_ai.action_razyyn_agent_settings",
    "razyyn_ai.action_razyyn_agent_write_policy",
    "razyyn_ai.action_razyyn_agent_write_log",
    "razyyn_ai.action_razyyn_agent_messaging_settings",
    "razyyn_ai.action_razyyn_agent_message_log",
)

# Everything an ordinary employee is meant to reach. The chat is theirs, and so
# are their own conversations -- narrowed to their own rows by the global
# record rules in security/security.xml, not by the menu.
_AN_EMPLOYEE_MAY_SEE = (
    "razyyn_ai.action_razyyn_chat",
    "razyyn_ai.action_razyyn_chat_session",
    "razyyn_ai.action_razyyn_chat_message",
)


@tagged("post_install", "-at_install")
class TestTheMenuAsItRenders(TransactionCase):

    def setUp(self):
        super().setUp()
        self.administrator = self.env.ref("base.user_admin")
        self.employee = self.env["res.users"].create({
            "name": "An Employee",
            "login": "razyyn-menu-test@example.com",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        # load_menus is cached per uid, and this transaction has just changed
        # who is in which group.
        self.env.registry.clear_cache()

    def _actions_in_the_menu_of(self, user):
        """Every action the web client would put in this person's menu."""
        self.env.registry.clear_cache()
        tree = self.env["ir.ui.menu"].with_user(user).load_menus(False)
        return {
            entry["action"] for entry in tree.values()
            if isinstance(entry, dict) and entry.get("action")
        }

    def _action_ref(self, xmlid):
        action = self.env.ref(xmlid)
        return f"{action._name},{action.id}"

    def test_the_administrator_sees_every_screen(self):
        visible = self._actions_in_the_menu_of(self.administrator)
        missing = [
            xmlid for xmlid in _EVERY_SCREEN
            if self._action_ref(xmlid) not in visible
        ]
        self.assertEqual(
            missing, [],
            "the administrator installs the module and cannot reach these: "
            + ", ".join(missing),
        )

    def test_the_administrator_is_a_razyyn_manager(self):
        """The reason the six were missing, asserted directly, so a failure
        says WHY rather than only that a menu is short."""
        self.assertTrue(
            self.administrator.has_group("razyyn_ai.group_razyyn_manager"),
        )

    def test_an_employee_reaches_the_chat_and_their_own_conversations(self):
        visible = self._actions_in_the_menu_of(self.employee)
        for xmlid in _AN_EMPLOYEE_MAY_SEE:
            with self.subTest(screen=xmlid):
                self.assertIn(self._action_ref(xmlid), visible)

    def test_an_employee_reaches_no_governance_screen(self):
        """Flattening moved `groups` from the headings onto the items. If it
        had been dropped instead, the menu would look identical to the
        administrator and this is the only thing that would say so."""
        visible = self._actions_in_the_menu_of(self.employee)
        for xmlid in (
            "razyyn_ai.action_razyyn_agent_write_policy",
            "razyyn_ai.action_razyyn_agent_write_log",
            "razyyn_ai.action_razyyn_agent_settings",
            "razyyn_ai.action_razyyn_agent_messaging_settings",
            "razyyn_ai.action_razyyn_agent_message_log",
        ):
            with self.subTest(screen=xmlid):
                self.assertNotIn(self._action_ref(xmlid), visible)

    def test_an_employee_is_left_with_no_empty_heading(self):
        """Odoo drops a parent whose children are all hidden -- but only if
        nothing else keeps it alive. Worth asserting, because the whole reason
        `groups` moved off the headings was to stop a section vanishing with
        its contents; the mirror mistake is a heading that stays with nothing
        under it."""
        tree = self.env["ir.ui.menu"].with_user(self.employee).load_menus(False)
        for entry in tree.values():
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            if entry.get("action"):
                continue
            with self.subTest(heading=entry.get("name")):
                self.assertTrue(
                    entry.get("children"),
                    f"'{entry.get('name')}' is an empty heading",
                )

    def test_the_app_opens_on_the_chat(self):
        """Clicking Razyyn AI must land on the chat, not on a list of models.
        The web client opens a root's first child by sequence."""
        root = self.env.ref("razyyn_ai.menu_razyyn_root")
        children = root.with_user(self.administrator).child_id.sorted(
            lambda menu: (menu.sequence, menu.id),
        )
        self.assertTrue(children)
        self.assertEqual(
            children[0], self.env.ref("razyyn_ai.menu_razyyn_chat"),
            f"the app would open on '{children[0].name}'",
        )
