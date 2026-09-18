# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Every message the agent sent, tried to send, or refused to send.

WHY REFUSALS AND FAILURES ARE IN HERE TOO
    An outbound record holding only successes cannot answer "did the agent
    email that to our client?", which is the question an auditor actually asks.
    A row is written for every attempt, whatever happened to it.

WHY THIS TABLE HAS NO UNIQUE KEY ON THE IDEMPOTENCY KEY
    It had one, and it was a trap with a delay fuse. The rule the service
    follows is that only a SENT attempt blocks a repeat — a refusal must be
    retryable, because the usual reason for one is a setting an administrator
    then goes and fixes. So the same key legitimately appears twice: once
    refused, once sent. With a unique key the second insert violated the
    constraint, and the writer is required never to raise (a logging failure
    must not turn a message that WAS delivered into an error the agent reports
    as a failure). The message would have gone out and left no record of
    itself, which is the one outcome this table exists to prevent.
"""

from odoo import fields, models


class AgentMessageLog(models.Model):
    _name = "razyyn.agent.message.log"
    _description = "Razyyn AI Agent Message Audit Log"
    _order = "timestamp desc, id desc"
    _rec_name = "idempotency_key"

    timestamp = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    sent_at = fields.Datetime(related="timestamp", string="Sent At", store=True)

    #: The agent's own three channel names. It sends `gmail`, and a selection
    #: that did not offer that value raised on insert — inside a writer that
    #: swallows what it raises, so a delivered message left no row behind.
    channel = fields.Selection([
        ("gmail", "Email"),
        ("telegram", "Telegram"),
        ("slack", "Slack"),
    ], required=True, index=True)

    #: Where it actually went: an email address, or a chat/channel id.
    #: NOT required — a send refused because the destination was not one this
    #: site lists has no address to record, and that attempt still owes a row.
    destination = fields.Char(string="Destination (Email / Chat ID)")
    recipient = fields.Char(related="destination", string="Recipient", store=True)
    #: The name the company gave that destination. This is what the agent puts
    #: in front of the accountant, so the log has to hold it too — "sent to
    #: 1131578960" is not something anybody can check.
    destination_label = fields.Char(string="Destination Name")

    subject = fields.Char(string="Subject")
    body = fields.Text(string="Message Body")
    attachment_names = fields.Char(string="Attachments")

    status = fields.Selection([
        ("sent", "Sent"),
        ("failed", "Failed"),
        ("refused", "Refused"),
    ], required=True, default="sent", index=True)

    #: The provider's OWN id for the message. The agent may tell a customer
    #: something was sent only when there is one of these; "nothing raised" is
    #: not evidence of delivery.
    provider_message_id = fields.Char(string="Provider Message ID")
    error_code = fields.Char(string="Error Code")
    error_message = fields.Text(string="Error Message")

    idempotency_key = fields.Char(string="Idempotency Key", index=True, required=True)
    session_id = fields.Char(string="Session ID", index=True)
    run_id = fields.Char(string="Run ID", index=True)
    requested_by = fields.Many2one("res.users", string="Requested By")
    approved_by = fields.Char(string="Approved By")
