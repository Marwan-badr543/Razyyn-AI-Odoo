# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""One authenticated caller: the Razyyn agent, for one company/user.

Mirrors Frappe Agent Settings:
- Holds user platform credentials (email, access_token, refresh_token)
- Manages 1-click connection state (erp_connection_id, write_recording_enabled, write_connected_on, write_last_error)
- Holds API key hash for inbound machine-to-machine calls (/agent_api/* and /agent_write/*)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_HASH_ITERATIONS = 210_000  # OWASP PBKDF2-SHA256 floor


def _hash_api_key(api_key: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", api_key.encode("utf-8"), salt, _HASH_ITERATIONS
    ).hex()


def _lookup_hash_api_key(api_key: str) -> str:
    """Plain SHA-256 of the plaintext key for fast O(1) indexed lookup."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class AgentSettings(models.Model):
    _name = "razyyn.agent.settings"
    _description = "Razyyn AI Agent Connection & Settings"
    _rec_name = "label"

    label = fields.Char(
        default="Razyyn AI Agent",
        help="A friendly label for this connection.",
    )
    user_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user,
        index=True, ondelete="cascade",
        help="The Odoo user who owns this connection.",
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        help="Which company's data this key may access.",
    )
    email = fields.Char(
        string="Razyyn Account Email",
        help="The username/email on the Razyyn Agent platform.",
    )
    access_token = fields.Char(
        groups="base.group_system",
        help="Short-lived JWT access token for the platform.",
    )
    refresh_token = fields.Char(
        groups="base.group_system",
        help="Long-lived JWT refresh token for renewing access tokens.",
    )
    custom_instructions = fields.Text(
        string="Custom Instructions",
        help="Accountant system prompt instructions sent with every chat turn.",
    )
    #: THE CARD HAS NOWHERE TO LIVE UNLESS A FIELD PUTS IT THERE.
    #:
    #: An Odoo form renders fields, so a widget needs one to hang on. This one
    #: holds nothing and is never read: the company's policy and its country
    #: live on the platform, which is what reads them at answer time. Storing
    #: a copy here would be a second answer to the same question, and the two
    #: would disagree the first time either changed.
    company_knowledge = fields.Char(
        string="Company Accounting Knowledge", store=False, readonly=True,
        compute="_compute_company_knowledge",
        help="Your accounting policy and the country whose rules the agent "
             "applies. Both are held by Razyyn AI, not in this database.",
    )

    def _compute_company_knowledge(self):
        for record in self:
            record.company_knowledge = False
    erp_connection_id = fields.Char(
        string="ERP Connection ID",
        help="Connection UUID registered with the platform.",
    )
    agent_erp_user_id = fields.Many2one(
        "res.users", string="Agent ERP User",
        help="Dedicated system user in Odoo representing the AI agent.",
    )
    write_recording_enabled = fields.Boolean(
        string="Allow Agent Recording", default=False,
        help="Platform-side toggle enabling the agent to record entries.",
    )
    write_connected_on = fields.Datetime(
        string="Connected On",
        help="Timestamp when ERP connection was verified with the platform.",
    )
    write_last_error = fields.Text(
        string="Last Verification Error",
        help="Error message if platform connection handshake failed.",
    )

    # Inbound Machine Authentication (PBKDF2-SHA256 salted hash)
    active = fields.Boolean(default=True)
    api_key_salt = fields.Char(readonly=True, groups="base.group_system")
    api_key_hash = fields.Char(
        readonly=True, index=True, groups="base.group_system",
        help="PBKDF2-SHA256 of the plaintext key.",
    )
    api_key_lookup_hash = fields.Char(
        readonly=True, index=True, groups="base.group_system",
        help="Plain SHA-256 lookup index.",
    )
    last_used = fields.Datetime(readonly=True)

    _sql_constraints = [
        ("api_key_hash_unique", "unique(api_key_hash)",
         "This API key is already in use by another connection."),
        ("api_key_lookup_hash_unique", "unique(api_key_lookup_hash)",
         "This API key is already in use by another connection."),
    ]

    @api.model
    def generate(self, user_id=None, company_id=None, label=None, email=None):
        """Create a connection and return (record, plaintext_api_key)."""
        plaintext = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        record = self.create({
            "label": label or "Razyyn AI Agent",
            "user_id": user_id or self.env.uid,
            "company_id": company_id or self.env.company.id,
            "email": email or "",
            "api_key_salt": salt.hex(),
            "api_key_hash": _hash_api_key(plaintext, salt),
            "api_key_lookup_hash": _lookup_hash_api_key(plaintext),
        })
        return record, plaintext

    def reissue_api_key(self) -> str:
        """Give this connection a new key and retire the old one at once.

        THE OLD KEY MUST STOP WORKING, and it is this method's existence that
        makes that true. Re-issuing used to mean CREATING ANOTHER CONNECTION:
        pressing Connect four times left four records, each holding a key that
        still authenticated. Rotating a credential that leaves the previous one
        live is not rotation -- it is handing out one more.
        """
        self.ensure_one()
        plaintext = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        self.write({
            "api_key_salt": salt.hex(),
            "api_key_hash": _hash_api_key(plaintext, salt),
            "api_key_lookup_hash": _lookup_hash_api_key(plaintext),
        })
        return plaintext

    @api.model
    def authenticate(self, api_key: str) -> Optional[models.Model]:
        """Resolve a plaintext API key to its connection record, or None."""
        if not api_key:
            return None

        lookup_hash = _lookup_hash_api_key(api_key)
        candidate = self.sudo().search([
            ("api_key_lookup_hash", "=", lookup_hash),
            ("active", "=", True),
        ], limit=1)
        if candidate:
            salt = bytes.fromhex(candidate.api_key_salt)
            computed = _hash_api_key(api_key, salt)
            if hmac.compare_digest(computed, candidate.api_key_hash):
                candidate.write({"last_used": fields.Datetime.now()})
                return candidate
            return None

        # Fallback for legacy records
        legacy_candidates = self.sudo().search([
            ("api_key_lookup_hash", "=", False),
            ("active", "=", True),
        ])
        for candidate in legacy_candidates:
            if not candidate.api_key_salt or not candidate.api_key_hash:
                continue
            salt = bytes.fromhex(candidate.api_key_salt)
            computed = _hash_api_key(api_key, salt)
            if hmac.compare_digest(computed, candidate.api_key_hash):
                candidate.write({
                    "last_used": fields.Datetime.now(),
                    "api_key_lookup_hash": lookup_hash,
                })
                return candidate
        return None

    @api.constrains("company_id")
    def _check_company_present(self):
        for record in self:
            if not record.company_id:
                raise ValidationError("An agent connection must belong to a company.")

    # NO `name` ALIAS, AND NO `allow_write_operations`/`last_synced_at`/
    # `last_error`/`agent_user_id` EITHER.
    #
    # Each of those was a second field `related=` to one that already existed,
    # carrying the same label. Odoo warns about every pair at install ("Two
    # fields ... have the same label"), and the cost is not the warning: two
    # spellings of one setting means the views were written against one and the
    # service layer against the other, so a change made in a view updated a
    # field no code read. `_rec_name = "label"` is what makes the connection
    # display by its label, which is all the `name` alias was for.
    is_platform_connected = fields.Boolean(
        string="Platform Connected", compute="_compute_is_platform_connected"
    )
    connection_status = fields.Char(
        string="Connection Status", compute="_compute_connection_status"
    )
    platform_api_base_url = fields.Char(
        string="Platform API Base URL", default="https://app.razyyn.com"
    )
    erp_base_url = fields.Char(
        string="ERP Base URL", compute="_compute_erp_base_url"
    )
    api_key = fields.Char(string="API Key", default="••••••••••••••••", readonly=True)
    api_key_prefix = fields.Char(string="Key Prefix", compute="_compute_api_key_prefix")

    # WHAT THE SERVER CAN AND CANNOT READ, shown where someone will see it.
    # The packages arrive with the module (post_init_hook -> services/
    # dependencies.py), so this is normally "Ready" and nobody thinks about it.
    # It is here for the cases that are not normal: no route to PyPI, a slim
    # image with pip stripped out, or tesseract absent -- each of which turns
    # every scanned invoice into a picture the model squints at, silently.
    document_reading_ready = fields.Boolean(
        string="Can Read Scans", compute="_compute_document_reading",
    )
    document_reading_status = fields.Text(
        string="Document Reading", compute="_compute_document_reading",
        help="Whether this server can read the words out of a scanned or "
             "photographed document, and what to install if it cannot.",
    )

    @api.depends("erp_connection_id")
    def _compute_is_platform_connected(self):
        for record in self:
            record.is_platform_connected = bool(record.erp_connection_id)

    @api.depends("erp_connection_id", "write_recording_enabled")
    def _compute_connection_status(self):
        for record in self:
            if not record.erp_connection_id:
                record.connection_status = "Disconnected"
            elif record.write_recording_enabled:
                record.connection_status = "Connected & Recording Enabled"
            else:
                record.connection_status = "Connected (Read Only)"

    def _compute_erp_base_url(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for record in self:
            record.erp_base_url = base_url

    @api.depends("api_key_lookup_hash")
    def _compute_api_key_prefix(self):
        for record in self:
            record.api_key_prefix = record.api_key_lookup_hash[:8] if record.api_key_lookup_hash else "None"

    def _compute_document_reading(self):
        """Read from the interpreter itself, never from a stored flag.

        A stored one would go on saying "Ready" after somebody rebuilt the
        container without the packages, which is precisely when it is read.
        """
        from ..services import dependencies

        state = dependencies.status()
        description = dependencies.describe()
        for record in self:
            record.document_reading_ready = state["ready"]
            record.document_reading_status = description

    def action_install_document_reading(self):
        """Fetch the reader's packages now, for an administrator who wants to retry.

        The same call the module install makes. Offered because the reasons it
        fails -- a proxy, a firewall, a mirror being down -- are all things
        fixed after the install and retried afterwards.
        """
        self.ensure_one()
        from ..services import dependencies

        report = dependencies.ensure()
        if report.ocr_ready:
            return self._notification(
                "Ready to read documents",
                "Scanned and photographed documents will be read as words.",
            )
        return self._notification(
            "Still cannot read documents", dependencies.describe(report), "warning",
        )


    @staticmethod
    def _notification(title, message, kind="success"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": kind != "success"},
        }

    def action_connect_platform(self):
        """Connect this Odoo to Razyyn AI, and SAY WHAT HAPPENED.

        This used to return True and show nothing at all. A connection whose
        credentials the platform could not verify looked exactly like one that
        worked -- the button simply stopped being offered -- and the customer
        only found out when the agent told them, a conversation later, that it
        was not connected to anything it could write to.
        """
        self.ensure_one()
        from ..services import agent_connection_service as conn_svc

        result = conn_svc.connect_write_access(
            self.env, self.user_id.id or self.env.uid,
            enable_recording=self.write_recording_enabled,
        )
        kind = "success" if result.get("verified") else "warning"
        return self._notification("Razyyn AI", result.get("message", ""), kind)

    def action_toggle_recording(self):
        """Turn the agent's ability to record entries on or off.

        Separate from connecting, and deliberately: reading a customer's books
        and writing to them are different permissions, and the second one is
        theirs to grant explicitly. The Write Policy in this Odoo still decides
        what may be written even once this is on.
        """
        self.ensure_one()
        from ..services import agent_connection_service as conn_svc

        result = conn_svc.set_recording_enabled(
            self.env, self.user_id.id or self.env.uid,
            not self.write_recording_enabled,
        )
        return self._notification("Razyyn AI", result.get("message", ""))

    def action_test_connection(self):
        self.ensure_one()
        from ..services import agent_connection_service as conn_svc
        status = conn_svc.get_write_connection_status(self.env, self.user_id.id or self.env.uid)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Connection Status",
                "message": status.get("message", "Connection active"),
                "type": "success" if status.get("connected") else "warning",
                "sticky": False,
            },
        }

    def action_rotate_keys(self):
        self.ensure_one()
        from ..services import agent_connection_service as conn_svc
        result = conn_svc.rotate_agent_credentials(
            self.env, self.user_id.id or self.env.uid
        )
        return self._notification(
            "Credentials Rotated", result.get("message", ""),
            "success" if result.get("verified") else "warning",
        )

    def action_disconnect_platform(self):
        self.ensure_one()
        from ..services import agent_connection_service as conn_svc
        conn_svc.disconnect_write_access(self.env, self.user_id.id or self.env.uid)
        return True

