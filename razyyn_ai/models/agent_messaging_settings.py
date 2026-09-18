# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""What this site may send through, and where it may send it.

THE SHAPE IS THE AGENT'S, NOT THIS MODULE'S
    ``agent/tools/messaging.py`` asks every ERP the same question — "what can
    you send through?" — and reads the answer with one piece of code
    (``describe_channels``). It knows three channels, spelled ``gmail``,
    ``telegram`` and ``slack``, and for the two bot channels it reads a list of
    destinations each carrying a ``label``. A site answering in any other
    vocabulary is a site the agent reports as having no message sending at all.

    So the fields here are named for what the agent needs, and the WhatsApp
    pair that used to sit beside them is gone: nothing in the product could
    ever have sent through it, and a switch that does nothing is worse than an
    absent one — an administrator turns it on and believes the agent can use it.

WHY DESTINATIONS ARE A LIST AND NOT A BOX TO TYPE A CHAT ID INTO
    A Telegram bot cannot start a conversation and a Slack bot cannot post in a
    channel it was never invited to. A chat id only exists after a person has
    pressed Start, so the set of places this site can reach is a list the
    company maintains, not something a language model may invent. The list is
    the whole permitted set, and the agent is given the labels so it can offer
    them by name.

    Email is the one that is not a permitted set. It reaches any address, so
    its list is an address book: save "Marwan" once and the accountant can ask
    for something to be emailed to Marwan instead of finding the address again
    every time. Typing a full address still works and always will.

WHY THERE IS EXACTLY ONE OF THESE RECORDS
    Every reader resolves this model through `get_settings_singleton`, which
    takes the first record there is. A second record would be a record nothing
    reads — configured with care and silently ignored — so `create` refuses to
    make one and the menu opens the existing one rather than a list.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

#: The channel keys on the wire. Spelled the agent's way — `gmail` is the email
#: channel whatever server actually carries it, because that is the key
#: `agent/tools/messaging.py` reads and the one its approval gate names.
CHANNELS = ("gmail", "telegram", "slack")


class AgentMessagingSettings(models.Model):
    _name = "razyyn.agent.messaging.settings"
    _description = "Razyyn AI Agent Messaging Settings"
    _rec_name = "name"

    name = fields.Char(
        string="Configuration Name",
        default="Agent Messaging Configuration",
        required=True,
    )

    # ── Email ────────────────────────────────────────────────────────────────
    #
    # TWO WAYS TO SEND, AND THE PRACTICE PICKS EITHER. Fill the mail server
    # boxes below and the agent signs in to that mailbox itself — a free Gmail
    # address with an App Password, an Outlook account, anything that speaks
    # SMTP. Leave them empty and it falls back to this Odoo's own outgoing mail
    # server, which is what a company that has already set one up expects.
    #
    # The agent calls the channel `gmail`; an administrator reading this form
    # sees "Email", because what matters to them is the mailbox it leaves from.
    email_enabled = fields.Boolean(
        string="Email Enabled", default=True,
        help="Let the agent send email on this company's behalf.",
    )
    email_from = fields.Char(
        string="Send Email From",
        help="The address email leaves from. Leave empty to use this system's default.",
    )
    email_sender_name = fields.Char(string="Sender Name")
    email_last_error = fields.Char(string="Last Email Error", readonly=True)

    smtp_host = fields.Char(
        string="Mail Server", groups="base.group_system",
        help="The outgoing mail server to sign in to — smtp.gmail.com for "
             "Gmail, smtp-mail.outlook.com for Outlook. Leave empty to send "
             "through this system's own outgoing mail server instead.",
    )
    smtp_port = fields.Integer(
        string="Port", groups="base.group_system",
        help="587 for STARTTLS (the usual choice), 465 for SSL. Leave it at 0 "
             "and the right one is used for the security setting.",
    )
    smtp_security = fields.Selection(
        [("starttls", "STARTTLS"), ("ssl", "SSL"), ("none", "None")],
        string="Security", default="starttls", groups="base.group_system",
        help="How the connection is encrypted. STARTTLS suits almost every "
             "provider, Gmail included.",
    )
    smtp_username = fields.Char(
        string="Mail Server Username", groups="base.group_system",
        help="Leave empty to sign in as the 'Send Email From' address, which "
             "is right for Gmail and most providers.",
    )
    smtp_password = fields.Char(
        string="Mail Server Password", groups="base.group_system",
        help="GMAIL WILL NOT ACCEPT YOUR NORMAL PASSWORD: turn on 2-Step "
             "Verification, then create a 16-character App Password at "
             "myaccount.google.com/apppasswords and paste it here. Only a "
             "system administrator can read or change it.",
    )

    # ── Telegram ─────────────────────────────────────────────────────────────
    telegram_enabled = fields.Boolean(string="Telegram Enabled", default=False)
    telegram_bot_token = fields.Char(
        string="Telegram Bot Token", groups="base.group_system",
        help="From BotFather. Only a system administrator can read or change it.",
    )
    telegram_last_error = fields.Char(string="Last Telegram Error", readonly=True)

    # ── Slack ────────────────────────────────────────────────────────────────
    slack_enabled = fields.Boolean(string="Slack Enabled", default=False)
    slack_bot_token = fields.Char(
        string="Slack Bot Token", groups="base.group_system",
        help="The bot user OAuth token (xoxb-…). Only a system administrator "
             "can read or change it.",
    )
    slack_last_error = fields.Char(string="Last Slack Error", readonly=True)

    # ── Where it may send ────────────────────────────────────────────────────
    #
    # ONE LIST UNDERNEATH, THREE BOOKS ON THE FORM. Every destination is a row
    # of the same model, so resolving a name, offering the names and refusing
    # an unknown one each have a single implementation. What changes per tab is
    # only which rows are shown and which channel a new row is filed under —
    # the `domain` decides the first and the `context` the second. Without that
    # context a row typed in the Slack tab would have no channel at all and be
    # refused, which is the loud version of the bug this replaced.
    destination_ids = fields.One2many(
        "razyyn.agent.messaging.destination", "settings_id",
        string="All Destinations",
    )
    email_destination_ids = fields.One2many(
        "razyyn.agent.messaging.destination", "settings_id",
        string="Saved Recipients",
        domain=[("channel", "=", "gmail")],
        context={"default_channel": "gmail"},
    )
    telegram_destination_ids = fields.One2many(
        "razyyn.agent.messaging.destination", "settings_id",
        string="Telegram Destinations",
        domain=[("channel", "=", "telegram")],
        context={"default_channel": "telegram"},
    )
    slack_destination_ids = fields.One2many(
        "razyyn.agent.messaging.destination", "settings_id",
        string="Slack Destinations",
        domain=[("channel", "=", "slack")],
        context={"default_channel": "slack"},
    )

    @api.model_create_multi
    def create(self, vals_list):
        """There is one messaging configuration, and only one.

        THIS IS NOT TIDINESS. Every reader of this model — the agent's config
        call, every send, the form the administrator fills in — resolves it
        through `get_settings_singleton`, which takes the first record there
        is. A second record is therefore a record that can be edited for ever
        and never used: an administrator saves a mailbox password into it,
        the agent goes on reading the other one, and the only symptom is that
        email "still is not set up". Refusing the second record is the only
        way that failure cannot happen.
        """
        vals_list = list(vals_list)
        if self.sudo().search_count([]) + len(vals_list) > 1:
            raise UserError(_(
                "There is one messaging configuration for this system and it "
                "already exists. Open it from Razyyn AI → Messaging Channels "
                "and edit it rather than adding a second one."
            ))
        return super().create(vals_list)

    @api.model
    def action_open_settings(self):
        """Open the one configuration record, making it if it is not there yet.

        The menu points at this rather than at a list, because a list of one
        row is a screen whose only purpose is to be clicked through — and whose
        "New" button creates the very second record that cannot be used.
        """
        settings = self.get_settings_singleton()
        return {
            "type": "ir.actions.act_window",
            "name": _("Messaging Channels"),
            "res_model": self._name,
            "view_mode": "form",
            "res_id": settings.id,
            "target": "current",
        }

    @api.model
    def get_settings_singleton(self):
        """The one configuration record, made on first use.

        ``sudo`` deliberately: the caller is the agent's own service
        environment and the record carries the site's tokens, which no
        accountant needs and this module never hands out.
        """
        settings = self.sudo().search([], limit=1)
        if not settings:
            settings = self.sudo().create({
                "name": "Agent Messaging Configuration",
                "email_enabled": True,
            })
        return settings

    def destinations_for(self, channel):
        """Every usable destination on one channel, in display order.

        Usable means it has both a name to call it by and an address to send
        to. A half-filled row is not offered to the agent, because offering a
        destination that cannot be reached turns a configuration mistake into a
        delivery failure the accountant sees.
        """
        self.ensure_one()
        return self.sudo().destination_ids.filtered(
            lambda row: row.channel == channel and row.label and row.address
        )
