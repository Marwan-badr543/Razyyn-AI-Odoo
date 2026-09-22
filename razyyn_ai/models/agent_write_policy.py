# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Agent Write Policy: Governs what the AI Agent may write into Odoo.

Mirrors Frappe Agent Write Policy:
- enabled: Master switch for agent write operations
- dry_run_only: Validates entries against ledger without committing
- require_approval: Requires human sign-off on planned actions
- restrict_to_listed_models: If True, only allowed_model_ids can be written to
- allowed_model_ids: Specific models permitted for write operations
- allowed_company_ids: Companies the agent may write into
- blocked_account_ids: General ledger accounts forbidden from modification
- max_documents_per_run: Maximum documents created in a single run (0 = unlimited)
- max_total_amount_per_run: Maximum total currency amount in a single run
- posting_date_max_days_back: Maximum days back for backdating entries
- posting_date_max_days_forward: Maximum days forward for future-dating (0 = forbidden)
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AgentWritePolicy(models.Model):
    _name = "razyyn.agent.write.policy"
    _description = "Razyyn AI Agent Write Policy"
    _rec_name = "display_name"

    display_name = fields.Char(default="Agent Write Governance Policy", readonly=True)
    enabled = fields.Boolean(
        string="Enable Agent Writes", default=False,
        help="Master switch authorizing the agent to create and modify accounting records.",
    )
    dry_run_only = fields.Boolean(
        default=False,
        help="When enabled, entries are validated against ledger constraints and discarded.",
    )
    require_approval = fields.Boolean(
        string="Require Human Approval", default=True,
        help="Require explicit user approval before executing any mutation.",
    )
    restrict_to_listed_models = fields.Boolean(
        string="Restrict to Listed Models", default=False,
        help="If checked, writes are strictly limited to models in Allowed Models list.",
    )
    allowed_model_ids = fields.Many2many(
        "ir.model", string="Allowed Models",
        help="Specific Odoo models the agent is authorized to write to.",
    )
    allowed_company_ids = fields.Many2many(
        "res.company", string="Allowed Companies",
        help="Companies the agent is permitted to write records for.",
    )
    blocked_account_ids = fields.Many2many(
        "account.account", string="Blocked Accounts",
        help="Accounts that must never be modified by AI operations (e.g., Retained Earnings).",
    )
    max_documents_per_run = fields.Integer(
        default=0,
        help="Maximum number of documents the agent can create in one turn (0 = unlimited).",
    )
    max_total_amount_per_run = fields.Float(
        default=0.0,
        help="Maximum monetary amount the agent can post in one run (0 = unlimited).",
    )
    posting_date_max_days_back = fields.Integer(
        string="Max Days Back", default=0,
        help="Maximum allowed days in the past for transaction posting dates.",
    )
    posting_date_max_days_forward = fields.Integer(
        string="Max Days Forward", default=0,
        help="Maximum allowed days in the future for transaction posting dates (0 = forbids future dates).",
    )
    name = fields.Char(string="Policy Name", default="Agent Write Governance Policy", required=True)

    max_amount_per_doc = fields.Float(
        string="Max Amount Per Document", default=0.0,
        help="Maximum amount for a single document (0 = unlimited).",
    )
    max_daily_amount = fields.Float(
        string="Max Daily Amount", related="max_total_amount_per_run", readonly=False, store=True
    )
    max_documents_per_batch = fields.Integer(
        string="Max Documents Per Batch", related="max_documents_per_run", readonly=False, store=True
    )
    earliest_posting_date = fields.Date(
        help="Entries dated before this date are forbidden.",
    )
    latest_posting_date = fields.Date(
        help="Entries dated after this date are forbidden.",
    )


    @api.model_create_multi
    def create(self, vals_list):
        """There is one write policy, and only one.

        THE SAME SILENT FAILURE THE MESSAGING CONFIGURATION HAD, and here it is
        a governance one. Every check resolves the policy through
        `get_policy_singleton`, which takes the first record there is — so a
        second policy is a screen where somebody can tighten every limit, save
        it, and change nothing at all about what the agent is allowed to write.
        """
        vals_list = list(vals_list)
        if self.sudo().search_count([]) + len(vals_list) > 1:
            raise UserError(_(
                "There is one write governance policy for this system and it "
                "already exists. Open it from Razyyn AI → Write Policy and "
                "edit it rather than adding a second one."
            ))
        return super().create(vals_list)

    @api.model
    def action_open_policy(self):
        """Open the one policy, making it if it is not there yet."""
        policy = self.get_policy_singleton()
        return {
            "type": "ir.actions.act_window",
            "name": _("Write Governance Policy"),
            "res_model": self._name,
            "view_mode": "form",
            "res_id": policy.id,
            "target": "current",
        }

    @api.model
    def get_policy_singleton(self):
        """Get or create the single write policy record."""
        policy = self.search([], limit=1)
        if not policy:
            policy = self.create({"enabled": False})
        return policy
