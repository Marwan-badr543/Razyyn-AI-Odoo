# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""One chat session the agent server is running, mirrored here so this app
can tell a clarification-question or a generated-file request belongs to it
before acting on it.

WHY THIS EXISTS AT ALL
    ``request_clarification`` and ``upload_file`` are both writes triggered
    by a ``session_id`` the caller supplies. Without a local record of which
    session ids are real and who they belong to, any holder of a valid API
    key could pass any other customer's session_id and attach a clarification
    or a file to a session that is not theirs — a cross-connection IDOR. The
    Frappe app closes the same hole with ``_assert_session_owned_by`` against
    its own ``Agent Chats`` doctype; this is that doctype's Odoo counterpart.
"""

from odoo import fields, models


class AgentChatSession(models.Model):
    _name = "razyyn.agent.chat.session"
    _description = "Razyyn Agent Chat Session"
    _rec_name = "session_id"

    session_id = fields.Char(
        required=True, index=True,
        help="The UUID the agent server uses as its own session/thread id. "
             "Created here the first time this connection mentions it, so "
             "an accountant's very first message in a new chat does not need "
             "a separate 'open a session' round trip.",
    )
    agent_settings_id = fields.Many2one(
        "razyyn.agent.settings", required=True, ondelete="cascade",
        help="Which connection owns this session — the identity a "
             "request_clarification/upload_file call is checked against.",
    )
    last_update = fields.Datetime(default=fields.Datetime.now)

    _sql_constraints = [
        ("session_id_unique", "unique(session_id)",
         "A session with this id already exists."),
    ]

    def touch(self):
        self.write({"last_update": fields.Datetime.now()})
