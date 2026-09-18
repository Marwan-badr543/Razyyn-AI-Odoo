# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Agent Write Log: Append-only audit trail of write actions executed by the agent.

Mirrors Frappe Agent Write Log:
- timestamp: When the write occurred
- user_id: Odoo user who initiated/authorized the run
- session_id: Conversation session identifier
- target_model: Model modified (e.g., 'account.move')
- target_record_id: ID of the record created or modified
- action_type: 'create', 'update', 'submit', 'cancel', 'amend', 'write'
- status: 'in_flight', 'success', 'rejected', 'failed', 'refused'
- payload: JSON serialized document payload
- rejection_reason: Reason if rejected by policy or validation
- idempotency_key: Unique key preventing duplicate writes
- response_data: JSON response data from write execution
- error_details: JSON traceback or error message on failure
- docstatus_written: 0 draft / 1 posted / 2 cancelled, as the protocol encodes it
- dry_run: True if executed in dry-run mode
"""

from odoo import fields, models


class AgentWriteLog(models.Model):
    _name = "razyyn.agent.write.log"
    _description = "Razyyn AI Agent Write Audit Log"
    _order = "timestamp desc, id desc"
    _rec_name = "idempotency_key"

    timestamp = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    user_id = fields.Many2one("res.users", string="User", index=True)
    session_id = fields.Char(string="Session ID", index=True)
    target_model = fields.Char(string="Target Model", index=True)
    target_record_id = fields.Integer(string="Target Record ID")
    action_type = fields.Selection([
        ("create", "Create"),
        ("update", "Update Draft"),
        ("submit", "Submit / Post"),
        ("cancel", "Cancel"),
        ("amend", "Amend / Update"),
        ("write", "Write"),
    ], required=True, default="create")
    status = fields.Selection([
        ("in_flight", "In Flight"),
        ("success", "Success"),
        ("rejected", "Rejected"),
        ("failed", "Failed"),
        ("refused", "Refused"),
    ], required=True, default="in_flight", index=True)
    docstatus_written = fields.Integer(
        string="Document State Written", default=0,
        help="Where the document stood after this action, in the write "
             "gateway protocol's encoding: 0 draft, 1 posted, 2 cancelled. "
             "Recorded because it is what the agent reads back to confirm an "
             "entry was actually POSTED rather than merely saved — without it "
             "a draft that was never posted is reported to the customer as "
             "recorded and done.",
    )
    payload = fields.Text(string="Payload (JSON)")
    rejection_reason = fields.Text(string="Rejection Reason")
    idempotency_key = fields.Char(string="Idempotency Key", index=True, required=True)
    response_data = fields.Text(string="Response Data (JSON)")
    error_details = fields.Text(string="Error Details")
    dry_run = fields.Boolean(string="Dry Run", default=False)

    _sql_constraints = [
        ("idempotency_key_unique", "unique(idempotency_key)",
         "An audit log entry with this idempotency key already exists."),
    ]
