# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""One message in a conversation thread.

Mirrors Frappe Agent Chat History:
- session_id: Reference to razyyn.agent.chat.session
- sender: 'human', 'ai', 'system'
- content: The message content (prose, plan JSON, or clarification JSON)
- agent_mode: 'ask', 'analyse', 'audit', 'auto'
- attachment_ids: Attached files
- created_at / creation1: Timestamp
"""

from odoo import fields, models


class AgentChatMessage(models.Model):
    _name = "razyyn.agent.chat.message"
    _description = "Razyyn AI Agent Chat Message"
    _order = "create_date asc, id asc"

    session_id = fields.Many2one(
        "razyyn.agent.chat.session", required=True, ondelete="cascade", index=True,
    )
    sender = fields.Selection(
        [("human", "Human"), ("ai", "AI Agent"), ("system", "System")],
        required=True, default="human",
    )
    content = fields.Text(required=True)
    agent_mode = fields.Selection(
        [("ask", "Ask"), ("analyse", "Analyse"), ("audit", "Audit"), ("auto", "Auto")],
        default="ask",
    )
    attachment_ids = fields.Many2many("ir.attachment", string="Attachments")
    raw_tool_calls = fields.Text(help="JSON string of tool calls or actions executed.")
    created_at = fields.Datetime(default=fields.Datetime.now, index=True)
    creation1 = fields.Datetime(default=fields.Datetime.now)
