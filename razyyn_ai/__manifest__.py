# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

{
    "name": "Razyyn AI",
    "version": "17.0.1.7.0",
    "summary": "The Razyyn AI accounting agent, inside Odoo: chat, reading, "
               "and governed recording of entries.",
    "description": """
Razyyn AI for Odoo 17
==========================================

The same product ERPNext customers use, on Odoo:

- The Razyyn chat window itself, streaming its work as it happens, with the
  transcript kept so a reload never loses a conversation
- Read-only SQL guard and dynamic schema introspection
- Recording of entries with ledger validation and strict written governance
- One-click connection to the Razyyn platform
- Append-only, tamper-evident write audit logs

The chat window is not a second implementation. It is the Frappe app's own,
copied file for file by tools/sync_from_frappe.py and given an Odoo to run on
by static/src/chat/razyyn_platform.js, so both products get every improvement
and neither can drift.
    """,
    "author": "Marwan Badr",
    "license": "LGPL-3",
    "category": "Accounting",
    "depends": ["base", "mail", "web", "account"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/chat_templates.xml",
        "views/agent_chat_views.xml",
        "views/agent_settings_views.xml",
        "views/agent_write_policy_views.xml",
        "views/agent_write_log_views.xml",
        "views/agent_messaging_views.xml",
        "views/model_browser_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        # Only the client action goes in Odoo's bundle. The chat window itself
        # is loaded by its own page inside the frame, deliberately: dropping
        # two and a half thousand lines of ERPNext stylesheet into Odoo's
        # bundle would restyle Odoo, and Odoo's Bootstrap would restyle the
        # chat.
        "web.assets_backend": [
            "razyyn_ai/static/src/backend/chat_action.js",
            "razyyn_ai/static/src/backend/chat_action.xml",
            "razyyn_ai/static/src/backend/chat_action.css",
        ],
    },
    # NO "external_dependencies" HERE, AND IT MUST NOT BE ADDED.
    # That field is a gate: ir.module.module.check_external_dependencies raises
    # before installation begins, so naming pytesseract there would refuse the
    # very install whose post_init_hook fetches pytesseract. The packages are
    # declared in services/dependencies.py, where something acts on them.
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": True,
}
