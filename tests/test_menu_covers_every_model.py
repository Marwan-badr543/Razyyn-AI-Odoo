# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Every model this module defines is reachable from the menu.

WHAT GOES WRONG WITHOUT THIS
    In Odoo a model is reachable only if some module put a menu in front of
    it. There is no awesome bar, no "open any DocType" box: a model with no
    menuitem cannot be opened by anybody, ever, and NOTHING reports that. The
    module installs, the table is created, the access rules are loaded, the
    tests pass, and the screen simply is not there.

    That is not hypothetical here. Six of this module's eight models were
    invisible to the administrator for a different reason (a `groups` on the
    heading above them), and the only way anyone found out was by looking at
    the menu and counting.

    So the source is the list: every `_name = "razyyn.…"` under models/ must
    have an action, and that action must be named by a menuitem. Add a model
    file and forget the menu, and this fails by name.

    THE ONE KIND OF MODEL THAT IS NOT OWED A MENU is a model that exists only
    as lines inside another model's form — a destination inside the messaging
    configuration, the same shape a child table has in Frappe. A screen of its
    own would be a second door onto the same rows, and a second door is how a
    row gets created under the wrong parent. Those are named in `_LINES_OF`
    below, and the exemption is not a free pass: the model they belong to must
    itself be reachable, and must actually hold the lines.

RUNS STANDALONE
    Pure text — no Odoo, no database. Reading the XML is enough to answer the
    question this asks, because "is there a menuitem pointing at this action"
    is a fact about the file. Whether that menu then RENDERS for a given
    person is a different question, gated by `groups` and by access rules, and
    it is answered against a live database in
    razyyn_ai/tests/test_menu_is_reachable.py. Neither test substitutes for
    the other.
"""

import os
import re
import unittest
import xml.etree.ElementTree as ElementTree

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON = os.path.join(_HERE, "..", "razyyn_ai")
_MODELS = os.path.join(_ADDON, "models")
_VIEWS = os.path.join(_ADDON, "views")

# `_name`, not `_inherit`: model_browser.py extends ir.model, which belongs to
# base and has a menu of its own making. Only what this module OWNS is owed a
# menu by this module.
_DECLARES = re.compile(r"""^\s*_name\s*=\s*["'](razyyn\.[^"']+)["']""", re.M)

#: Line models, and the model whose form holds them. Reachable through that
#: form and deliberately nowhere else.
_LINES_OF = {
    "razyyn.agent.messaging.destination": "razyyn.agent.messaging.settings",
}


def _models_declared():
    found = {}
    for name in sorted(os.listdir(_MODELS)):
        if not name.endswith(".py") or name == "__init__.py":
            continue
        path = os.path.join(_MODELS, name)
        with open(path, encoding="utf-8") as handle:
            for model in _DECLARES.findall(handle.read()):
                found[model] = name
    return found


def _view_roots():
    for name in sorted(os.listdir(_VIEWS)):
        if name.endswith(".xml"):
            yield name, ElementTree.parse(os.path.join(_VIEWS, name)).getroot()


def _actions_by_model():
    """XML id -> model, for every action in views/ that opens one.

    BOTH KINDS COUNT. A window action names its model in `res_model`; a server
    action names it as `model_id`, by the XML id `model_<underscored name>`
    that Odoo gives every model. The messaging configuration is opened by a
    server action on purpose — it is a single record, and a list of one row
    carries a New button that makes a second record nothing will read. Reading
    only `res_model` would have reported that screen as missing.
    """
    actions = {}
    for _name, root in _view_roots():
        for record in root.iter("record"):
            kind = record.get("model")
            if kind not in ("ir.actions.act_window", "ir.actions.server"):
                continue
            for field in record.iter("field"):
                if kind == "ir.actions.act_window" and field.get("name") == "res_model":
                    actions[record.get("id")] = (field.text or "").strip()
                elif kind == "ir.actions.server" and field.get("name") == "model_id":
                    reference = (field.get("ref") or "").strip()
                    if reference.startswith("model_"):
                        actions[record.get("id")] = \
                            reference[len("model_"):].replace("_", ".")
    return actions


def _actions_named_by_a_menu():
    named = set()
    for _name, root in _view_roots():
        for item in root.iter("menuitem"):
            action = item.get("action")
            if action:
                named.add(action)
    return named


class TestEveryModelIsInTheMenu(unittest.TestCase):

    def setUp(self):
        self.models = _models_declared()
        self.actions = _actions_by_model()
        self.menued = _actions_named_by_a_menu()

    def test_the_module_still_has_its_models(self):
        # A regex that matches nothing would make every other case below pass
        # for the wrong reason.
        self.assertGreaterEqual(len(self.models), 8, self.models)

    def test_every_model_has_an_action(self):
        opened = set(self.actions.values())
        for model, source in sorted(self.models.items()):
            if model in _LINES_OF:
                continue
            with self.subTest(model=model):
                self.assertIn(
                    model, opened,
                    f"{source} declares {model} and no view opens it. Add an "
                    f"ir.actions.act_window with res_model={model}.",
                )

    def test_every_model_is_named_by_a_menuitem(self):
        reachable = {
            model for action, model in self.actions.items()
            if action in self.menued
        }
        for model, source in sorted(self.models.items()):
            if model in _LINES_OF:
                continue
            with self.subTest(model=model):
                self.assertIn(
                    model, reachable,
                    f"{source} declares {model}, and its action is in no "
                    f"menuitem. Nobody can open it. Add it to views/menus.xml.",
                )

    def test_a_line_model_is_held_by_a_model_that_is_reachable(self):
        """The exemption has to earn itself. Lines with no form holding them
        are lines nobody can ever type, which is the very thing this file
        exists to catch."""
        reachable = {
            model for action, model in self.actions.items()
            if action in self.menued
        }
        for lines, holder in sorted(_LINES_OF.items()):
            with self.subTest(model=lines):
                self.assertIn(lines, self.models, f"{lines} is not declared")
                self.assertIn(
                    holder, reachable,
                    f"{lines} is only reachable through {holder}, and {holder} "
                    f"is not in the menu.",
                )
                source = os.path.join(_MODELS, self.models[holder])
                with open(source, encoding="utf-8") as handle:
                    body = handle.read()
                self.assertIn(
                    f'"{lines}"', body,
                    f"{self.models[holder]} does not hold any {lines} lines.",
                )

    def test_the_chat_opens_first(self):
        """The app icon opens its lowest-sequence child, so the chat has to be
        it. A model list is not what someone clicking "Razyyn AI" wants."""
        under_root = {}
        for _name, root in _view_roots():
            for item in root.iter("menuitem"):
                if item.get("parent") == "menu_razyyn_root":
                    under_root[item.get("id")] = int(item.get("sequence", "10"))
        self.assertIn("menu_razyyn_chat", under_root)
        first = min(under_root, key=lambda item: under_root[item])
        self.assertEqual(
            first, "menu_razyyn_chat",
            f"clicking the app would open {first}, not the chat",
        )

    def test_no_menu_hangs_off_a_parent_that_is_gone(self):
        """Flattening the three sections into one deleted two headings. A
        child left pointing at a deleted parent is an XML id Odoo cannot
        resolve, and the module stops installing."""
        declared, referenced = set(), {}
        for name, root in _view_roots():
            for item in root.iter("menuitem"):
                declared.add(item.get("id"))
                if item.get("parent"):
                    referenced[item.get("id")] = (item.get("parent"), name)
        for child, (parent, name) in sorted(referenced.items()):
            with self.subTest(menu=child):
                self.assertIn(parent, declared, f"{name}: {child} -> {parent}")


if __name__ == "__main__":
    unittest.main()
