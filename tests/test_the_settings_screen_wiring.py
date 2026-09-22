# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Two things about the Agent Settings screen that fail silently when broken.

RUNS STANDALONE
    Pure text — no Odoo, no database. Both questions below are facts about the
    files, and both were live failures answered by reading a file.
"""

import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE = os.path.join(os.path.dirname(_HERE), "razyyn_ai")


def _read(*parts):
    with open(os.path.join(_MODULE, *parts), encoding="utf-8") as handle:
        return handle.read()


def _text_of(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _python_files():
    for root, dirs, names in os.walk(_MODULE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            if name.endswith(".py"):
                yield os.path.join(root, name)


class TranslationIdiomIsOdooSeventeens(unittest.TestCase):
    """`self.env._(...)` DOES NOT EXIST BEFORE ODOO 18.

    This is not a style preference. Opening Razyyn AI → Write Policy raised

        AttributeError: 'Environment' object has no attribute '_'

    from `action_open_policy`, and the same idiom sat on around forty more
    error paths across six files — each one a crash waiting for the condition
    that reaches it, on paths nobody exercises until something has already gone
    wrong. pylint-odoo's `prefer-env-translation` is what put it there; that
    check is disabled in `.pylintrc`, with the reason, because this module is
    written for Odoo 17 and the Odoo 18 tree is GENERATED from it.

    `from odoo import _` is correct on both series — 17 and 18 each export it
    from `odoo.tools.translate`, and 17's takes the same %(name)s keyword
    arguments — so one spelling serves both trees.
    """

    def test_no_file_calls_the_odoo_18_only_translation_helper(self):
        offenders = [
            path for path in _python_files()
            if re.search(r"\benv\._\(", _text_of(path))
        ]
        self.assertEqual(
            [], [os.path.relpath(p, _MODULE) for p in offenders],
            "these call env._(), which raises AttributeError on Odoo 17",
        )

    def test_every_file_that_translates_has_the_helper_in_scope(self):
        """The other half, and the one a careless fix creates.

        Rewriting `self.env._(...)` to `_(...)` without adding `_` to the
        import turns an AttributeError into a NameError: the same crash, on the
        same unexercised paths, wearing a different name.
        """
        missing = []
        for path in _python_files():
            text = _text_of(path)
            if not re.search(r"[^A-Za-z0-9_.]_\(", text):
                continue
            if not re.search(r"^from odoo import .*\b_\b", text, re.MULTILINE):
                missing.append(os.path.relpath(path, _MODULE))
        self.assertEqual([], missing, "these call _() without importing it")


class ThePlanUsageCardIsWiredUp(unittest.TestCase):
    """A widget is reachable only if four separate files agree.

    Each of the four fails differently and none of them fails loudly: a field
    with no view renders nothing, a view naming an unregistered widget renders
    an empty box, a source file left out of the bundle is never served, and a
    bundle change on an unbumped version is served from the cache that was
    built before it existed.
    """

    WIDGET = "razyyn_plan_usage"
    PRICING = "https://razyyn.com/pricing/"

    def test_the_field_the_card_hangs_on_exists(self):
        self.assertIn("plan_usage = fields.Char(", _read("models", "agent_settings.py"))

    def test_the_form_places_the_field_and_names_the_widget(self):
        view = _read("views", "agent_settings_views.xml")
        self.assertIn('name="plan_usage"', view)
        self.assertIn(f'widget="{self.WIDGET}"', view)

    def test_the_widget_registers_under_the_name_the_view_asks_for(self):
        source = _read("static", "src", "backend", "plan_usage.js")
        self.assertIn(f'registry.category("fields").add("{self.WIDGET}"', source)

    def test_every_source_file_of_the_card_is_in_the_backend_bundle(self):
        manifest = _read("__manifest__.py")
        for suffix in ("js", "xml", "css"):
            self.assertIn(f"razyyn_ai/static/src/backend/plan_usage.{suffix}", manifest)

    def test_the_upgrade_link_points_at_the_pricing_page(self):
        """The one thing on the card a customer is meant to act on.

        Hard-coded once per product; the Frappe app names the same address once
        in its own settings script.
        """
        source = _read("static", "src", "backend", "plan_usage.js")
        self.assertIn(self.PRICING, source)
        template = _read("static", "src", "backend", "plan_usage.xml")
        self.assertIn('target="_blank"', template)
        self.assertIn('rel="noopener noreferrer"', template)

    def test_the_route_the_card_fetches_from_is_declared(self):
        controller = _read("controllers", "usage_api.py")
        self.assertIn('"/razyyn/usage"', controller)
        self.assertIn('type="http"', controller)
        self.assertIn('auth="user"', controller)
        self.assertIn("from . import usage_api", _read("controllers", "__init__.py"))


if __name__ == "__main__":
    unittest.main()
