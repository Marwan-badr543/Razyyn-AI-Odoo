# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""One place the agent is allowed — or knows how — to send a message.

WHY THIS IS A RECORD AND NOT A COMMA-SEPARATED BOX
    The agent is told the LABEL and never the address. That is what lets an
    accountant write "send it to the finance group" and what stops a model
    inventing a chat id — an unlisted destination is refused rather than
    attempted. A comma-separated list of raw ids carries no labels, so there
    would be nothing to offer and nothing to refuse against.

THE SAME ROW SERVES THREE CHANNELS, AND MEANS TWO DIFFERENT THINGS
    On Telegram and Slack the list is the WHOLE PERMITTED SET: a bot cannot
    start a conversation, so anything not on the list cannot be reached and is
    refused. On email the list is an ADDRESS BOOK: email reaches any address,
    so a saved row only saves the accountant from typing "marwan@…" every time
    — "send it to Marwan" resolves, and a typed address still works.

    Both live in one model because both answer the same question — "what is
    this destination called, and what does that name stand for?" — and because
    the resolution, the display and the refusal wording then have one
    implementation each rather than three.

WHY THERE IS NO DEFAULT CHANNEL
    There used to be, and it was a trap. Each tab of the configuration form
    creates rows for its own channel and does not show the channel column, so a
    row created without one was silently filed under the default: a Slack
    destination typed in the Slack tab vanished from it and Slack went on
    reporting that it had no destinations. Required and undefaulted, a caller
    that forgets to say which channel is refused loudly instead.
"""

from odoo import api, fields, models

#: The three channels a destination can belong to, spelled the way the agent
#: spells them. ``gmail`` is the email channel whatever mailbox carries it.
DESTINATION_CHANNELS = [
    ("gmail", "Email"),
    ("telegram", "Telegram"),
    ("slack", "Slack"),
]


class AgentMessagingDestination(models.Model):
    _name = "razyyn.agent.messaging.destination"
    _description = "Razyyn AI Agent Messaging Destination"
    _order = "channel, sequence, id"
    _rec_name = "label"

    settings_id = fields.Many2one(
        "razyyn.agent.messaging.settings", string="Configuration",
        required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    channel = fields.Selection(DESTINATION_CHANNELS, required=True)
    label = fields.Char(
        string="Name", required=True,
        help="What the accountant calls this destination — 'Finance team', "
             "'Marwan'. This is the only part the agent is ever told.",
    )
    address = fields.Char(
        required=True,
        help="Email: the address itself. Telegram: the numeric chat id. "
             "Slack: the channel id (C…). A bot can only reach a chat that "
             "already exists.",
    )
    is_default = fields.Boolean(
        string="Default",
        help="Where a message goes when the accountant names no destination.",
    )
    notes = fields.Char()

    @api.model_create_multi
    def create(self, vals_list):
        rows = super().create(vals_list)
        rows._settle_the_default()
        return rows

    def write(self, vals):
        result = super().write(vals)
        if "is_default" in vals or "channel" in vals:
            self._settle_the_default()
        return result

    def _settle_the_default(self):
        """One default per channel, settled whenever a row is saved.

        Two defaults is not an error anyone would notice on the form; it is a
        message going to the wrong colleague weeks later. The service resolves
        a default by taking the first one it finds, so THE LAST ONE TICKED WINS
        and there is only ever one to find: across saves that is the row just
        written, and within one save the last row in the list. Both ERPs settle
        it the same way, because a rule two products apply differently is a rule
        neither of them really has.

        ON SAVE AND NOT ONLY ON THE FORM. As an onchange this settled what was
        typed and nothing else — a row created by a data import, by a
        migration, or from the standalone list carried a second default that
        nothing would clear, and the symptom is a report emailed to the wrong
        person months later.
        """
        # Last first, so that the last row ticked is the one left standing.
        for row in self.filtered("is_default")[::-1]:
            if not row.is_default:
                # Cleared by an earlier turn of this same loop. Two rows ticked
                # in ONE write used to clear each other and leave the channel
                # with no default at all — the resolver then asked which one to
                # use, over a list where somebody had plainly chosen.
                continue
            siblings = row.settings_id.destination_ids.filtered(
                lambda other: (
                    other.channel == row.channel
                    and other != row
                    and other.is_default
                )
            )
            if siblings:
                siblings.write({"is_default": False})

    #: The configuration field each channel's lines are edited through. Used
    #: only while a form is open: the whole list, `destination_ids`, is not on
    #: the form any more, so an onchange reaching for it would find nothing and
    #: quietly stop correcting anything.
    _FIELD_BY_CHANNEL = {
        "gmail": "email_destination_ids",
        "telegram": "telegram_destination_ids",
        "slack": "slack_destination_ids",
    }

    @api.onchange("is_default")
    def _onchange_is_default(self):
        """The same thing while it is being typed, so the form shows it."""
        for row in self.filtered("is_default"):
            field = self._FIELD_BY_CHANNEL.get(row.channel)
            if not field:
                continue
            for other in row.settings_id[field]:
                if other != row and other.is_default:
                    other.is_default = False
