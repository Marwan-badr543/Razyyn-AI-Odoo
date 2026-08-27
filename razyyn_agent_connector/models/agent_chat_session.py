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

from odoo import api, fields, models


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

    @api.model
    def cron_cleanup_expired_generated_files(self):
        """Entry point for the hourly ``ir.cron`` job.

        ``ir.cron``'s ``code`` field runs through Odoo's restricted
        safe_eval sandbox, which forbids ``import``/``from ... import``
        outright (``forbidden opcode(s) ... IMPORT_NAME, IMPORT_FROM`` —
        found by actually installing this module on a live Odoo 17, not by
        review). The cron's code is therefore just ``model.
        cron_cleanup_expired_generated_files()``, using the ``model``
        the sandbox already binds from this method's own ``model_id`` —
        no import needed at that call site. The import happens here
        instead, in an ordinary Python module Odoo loads normally at
        install time, which is a completely different code path from the
        cron sandbox.
        """
        from ..services import agent_api_service

        return agent_api_service.cleanup_old_files(self.env)
