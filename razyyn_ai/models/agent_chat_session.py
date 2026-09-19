# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""One chat session the agent server is running.

Mirrors Frappe Agent Chats:
- session_id: UUID string
- title: string
- user_id: Owner of the chat session
- last_update: Timestamp of the last interaction
- message_ids: Messages in this conversation thread
"""

import uuid
from odoo import api, fields, models


class AgentChatSession(models.Model):
    _name = "razyyn.agent.chat.session"
    _description = "Razyyn AI Agent Chat Session"
    _order = "last_update desc, create_date desc"
    _rec_name = "title"

    session_id = fields.Char(
        required=True, index=True, default=lambda: str(uuid.uuid4()),
        help="The UUID identifying this conversation thread.",
    )
    title = fields.Char(
        default="New Conversation", required=True,
        help="Display title of the conversation thread.",
    )
    user_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user,
        index=True, ondelete="cascade",
        help="The Odoo user who owns this chat thread.",
    )
    last_update = fields.Datetime(default=fields.Datetime.now, index=True)
    message_ids = fields.One2many(
        "razyyn.agent.chat.message", "session_id", string="Messages",
    )
    agent_settings_id = fields.Many2one(
        "razyyn.agent.settings", ondelete="set null",
        help="Optional reference to connection settings.",
    )
    backend_session_id = fields.Char(
        help=(
            "The thread id the agent server knows this conversation by. Ordinarily "
            "the same string as session_id; editing a message rotates it so the "
            "desk's own state starts clean while everything stored here keeps its "
            "identity."
        ),
    )
    cancel_requested = fields.Boolean(
        default=False, copy=False,
        help=(
            "Set the moment the customer presses stop, and cleared when they send "
            "again. Recorded before anybody is asked, because the turn may not have "
            "left this server yet -- a stop pressed in that window must still stop it."
        ),
    )

    def get_backend_session_id(self):
        self.ensure_one()
        return self.backend_session_id or self.session_id

    _sql_constraints = [
        ("session_id_unique", "unique(session_id)",
         "A session with this id already exists."),
    ]

    message_count = fields.Integer(
        compute="_compute_message_count",
        help="How many turns this conversation holds.",
    )

    @api.depends("message_ids")
    def _compute_message_count(self):
        # Counted in one grouped query rather than one per row: a practice with
        # a few hundred conversations would otherwise pay a query per line of
        # the list view.
        counts = dict(self.env["razyyn.agent.chat.message"]._read_group(
            [("session_id", "in", self.ids)], ["session_id"], ["__count"],
        ) or [])
        for session in self:
            found = counts.get(session)
            session.message_count = found if isinstance(found, int) else len(session.message_ids)

    def action_open_in_chat(self):
        """Open this conversation in the chat window itself."""
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("razyyn_ai.action_razyyn_chat")
        # The chat picks its conversation up from the address, so a customer
        # arriving from this button lands in the right thread rather than in
        # whichever one they had open last.
        action["params"] = {"session_id": self.session_id}
        return action

    def touch(self):
        self.write({"last_update": fields.Datetime.now()})

    @api.model
    def cron_cleanup_expired_generated_files(self):
        """Entry point for the hourly ir.cron cleanup job."""
        from ..services import agent_api_service, ocr_service

        removed = agent_api_service.cleanup_old_files(self.env)
        # The readings taken from photographed invoices live beside their
        # working copies, and neither belongs to a record -- so nothing else
        # would ever remove them.
        ocr_service.prune_working_copies(self.env)
        return removed
