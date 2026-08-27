# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""A clarification question the agent asked, or a report it generated,
recorded against a session — the Odoo counterpart of Frappe's
``Agent Chat History``.

This module does not render a chat UI. It exists so ``request_clarification``
has somewhere durable to write, and so a support conversation about "the agent
asked me something odd on Tuesday" has a record to look at — a UI reading this
table is future work, out of scope for the connector itself.
"""

from odoo import fields, models


class AgentChatMessage(models.Model):
    _name = "razyyn.agent.chat.message"
    _description = "Razyyn Agent Chat Message"
    _order = "create_date desc"

    session_id = fields.Many2one(
        "razyyn.agent.chat.session", required=True, ondelete="cascade", index=True,
    )
    sender = fields.Selection(
        [("agent", "Agent"), ("system", "System")], required=True, default="agent",
        help="Never 'user' — the accountant's own messages live in the chat "
             "UI on the razyyn-frappe-15/agent-facing frontend, not here. "
             "This table only ever records what the agent asked or produced.",
    )
    kind = fields.Selection(
        [("clarification", "Clarification"), ("generated_file", "Generated File")],
        required=True,
    )
    content = fields.Text(
        help="JSON payload — the clarification questions, or the attachment "
             "reference for a generated file.",
    )
    attachment_id = fields.Many2one("ir.attachment", ondelete="set null")
