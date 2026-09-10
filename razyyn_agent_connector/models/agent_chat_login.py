# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""One Odoo user's own sign-in to the Razyyn platform, from the chat page.

Odoo's counterpart to the Frappe app's ``Agent Settings`` doctype
(``razyyn-frappe-15/accountant_agent/.../doctype/agent_settings``): a row per
END USER who has connected their own Razyyn account through
``/razyyn/chat``, holding the JWT pair the chat page's requests carry.

NOT the same thing as ``razyyn.agent.settings``. That model is the API key
the *razyyn server* presents when it calls back into this Odoo instance
(``/agent_api/execute_query`` etc.) — one per connection, machine-to-machine.
This model is the credential *this Odoo user's browser* presents when it
calls out to the razyyn server's own ``/agent/ask`` — human-to-platform,
one row per Odoo user. A sign-up creates one of each: a fresh
``razyyn.agent.settings`` row (so the razyyn server has something to read
this company's data with) and one row here (so this Odoo user's chat page
knows it is signed in).

ONE ROW PER ODOO USER, NOT PER (ODOO USER, RAZYYN ACCOUNT)
    A second Odoo user who tries to sign up hits the razyyn server's own
    one-account-per-ERP-instance guard (see ``api/services/erp_identity.py``
    on the razyyn side) — the identity fingerprint is computed from this
    company's own data (``res_company`` name/creation date), which is the
    same regardless of which Odoo user is asking. So today only the first
    Odoo user at a given company can complete Sign Up; that is an existing
    platform constraint this page surfaces (a 409 from the server, forwarded
    verbatim), not one introduced here.
"""

from odoo import fields, models


class AgentChatLogin(models.Model):
    _name = "razyyn.chat.login"
    _description = "Razyyn Chat Login (per Odoo user)"
    _rec_name = "email"

    user_id = fields.Many2one(
        "res.users", required=True, ondelete="cascade", index=True,
        help="The Odoo user this sign-in belongs to.",
    )
    email = fields.Char(
        required=True,
        help="The Razyyn account's own username — not necessarily this "
             "Odoo user's email, though the chat page's Sign Up form "
             "defaults to it.",
    )
    access_token = fields.Char(
        groups="base.group_system",
        help="Short-lived JWT the chat page sends on every /agent/* call.",
    )
    refresh_token = fields.Char(
        groups="base.group_system",
        help="Exchanged for a fresh access_token on a 401 — see "
             "controllers/chat_controller.py's refresh-on-401 handling. "
             "Rotates: the razyyn server invalidates the old one on each use.",
    )
    agent_settings_id = fields.Many2one(
        "razyyn.agent.settings", ondelete="set null",
        help="The connection this sign-up minted for the razyyn server to "
             "read this company's data with.",
    )

    _sql_constraints = [
        ("user_id_unique", "unique(user_id)",
         "This Odoo user already has a Razyyn chat login."),
    ]
