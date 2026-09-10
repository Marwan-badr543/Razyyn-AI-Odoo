# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""One authenticated caller: the razyyn agent, for one company.

DELIBERATE DEPARTURE FROM THE FRAPPE APP'S DESIGN
    ``Agent Settings`` on the Frappe side stores the API key encrypted and
    reversible, because ``find_settings_name_by_api_key`` there both narrows by
    a hash AND decrypts the stored value for a constant-time compare. Odoo has
    no equivalent of Frappe's ``Password`` fieldtype /
    ``get_decrypted_password`` out of the box, and reversible encryption is not
    actually required here — nothing needs the plaintext key back once it has
    been issued. So this stores a salted hash only, the same shape as a user
    password: the caller sees the key exactly once, at creation, and never
    again. That is a strictly smaller attack surface than a reversible secret,
    not a shortcut.
"""

import hashlib
import hmac
import secrets

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_HASH_ITERATIONS = 210_000  # OWASP's current PBKDF2-SHA256 floor (2023 guidance)


def _hash_api_key(api_key: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", api_key.encode("utf-8"), salt, _HASH_ITERATIONS
    ).hex()


def _lookup_hash_api_key(api_key: str) -> str:
    """Plain SHA-256 of the plaintext key — an O(1) index, NOT the credential
    hash ``authenticate`` actually trusts (that stays PBKDF2-SHA256 via
    ``_hash_api_key``, salted, unchanged by this).

    Security audit 2026-09, finding #3: plain SHA-256 (no HMAC/pepper) is
    deliberate here, not a shortcut — the input is
    ``secrets.token_urlsafe(32)`` (256 bits of real randomness), never a
    human password, so there is no dictionary/rainbow-table risk a slow hash
    or a server-side pepper would be defending against. A pepper (e.g. keyed
    off ``ir.config_parameter``'s ``database.secret``) was considered and
    rejected: it would make every stored lookup hash permanently
    unmatchable the moment that parameter is absent or rotated, with no
    recovery path, for a defense this key's own entropy already makes
    unnecessary.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class AgentSettings(models.Model):
    _name = "razyyn.agent.settings"
    _description = "Razyyn Agent Connection"
    _rec_name = "label"

    label = fields.Char(
        default="Razyyn Agent",
        help="A name for this connection, shown in the list — useful once a "
             "site has more than one (a sandbox key alongside a live one).",
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        help="Which company's data this key may read. A query authenticated "
             "by this key is scoped to this company the same way any other "
             "Odoo read is — see agent_api_service.py.",
    )
    active = fields.Boolean(default=True)
    api_key_salt = fields.Char(required=True, readonly=True, groups="base.group_system")
    api_key_hash = fields.Char(
        required=True, readonly=True, index=True, groups="base.group_system",
        help="PBKDF2-SHA256 of the plaintext key. The plaintext itself is "
             "never stored — see the module docstring.",
    )
    api_key_lookup_hash = fields.Char(
        readonly=True, index=True, groups="base.group_system",
        help="Plain SHA-256 of the plaintext key — an O(1) index used only "
             "to FIND the one candidate record worth PBKDF2-verifying "
             "(see api_key_hash), never to authenticate by itself. "
             "Security audit 2026-09, finding #3: without this, "
             "`authenticate` had to run a fresh PBKDF2-SHA256 @ 210k "
             "iterations against every active connection's row, for every "
             "incoming call, before any credential was known good — an "
             "unauthenticated CPU-exhaustion DoS that scaled with the "
             "number of connections. Left blank on records created before "
             "this fix (the plaintext key cannot be recovered to backfill "
             "it after the fact); `authenticate` self-heals those on their "
             "first successful use — see its own comment.",
    )
    last_used = fields.Datetime(readonly=True)

    _sql_constraints = [
        ("api_key_hash_unique", "unique(api_key_hash)",
         "This API key is already in use by another connection."),
        ("api_key_lookup_hash_unique", "unique(api_key_lookup_hash)",
         "This API key is already in use by another connection."),
    ]

    @api.model
    def generate(self, company_id=None, label=None):
        """Create a connection and return ``(record, plaintext_api_key)``.

        The plaintext is returned exactly once. If the caller loses it, the
        remedy is a new connection, not a lookup — nothing in this model can
        recover it, by design.
        """
        plaintext = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        record = self.create({
            "label": label or "Razyyn Agent",
            "company_id": company_id or self.env.company.id,
            "api_key_salt": salt.hex(),
            "api_key_hash": _hash_api_key(plaintext, salt),
            "api_key_lookup_hash": _lookup_hash_api_key(plaintext),
        })
        return record, plaintext

    @api.model
    def authenticate(self, api_key):
        """Resolve a plaintext API key to its connection record, or ``None``.

        Constant-time on the candidate's own hash comparison (``hmac.compare_
        digest``); the salt lookup itself is an ordinary indexed read and is
        not required to be constant-time — it reveals nothing about a key an
        attacker does not already hold, and every active record's salt is
        equally reachable regardless of what the attacker sends.

        Security audit 2026-09, finding #3: an indexed, O(1)
        ``api_key_lookup_hash`` match finds at most one candidate before any
        PBKDF2 work happens, so this no longer PBKDF2-hashes every active
        connection's row per incoming call — the CPU-exhaustion DoS that
        scaling implied. PBKDF2 still runs once as the actual credential
        check on whatever the O(1) lookup finds, unchanged.
        """
        if not api_key:
            return None

        # Read as sudo: this IS the authentication check, run before any
        # notion of "which user" exists — there is no user yet to check
        # permissions as. Every column read here is deliberately not a
        # secret (label, company, salt); the hash itself is compared, never
        # returned.
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

        # Legacy fallback: records created before this fix have no
        # api_key_lookup_hash to match on (the plaintext key cannot be
        # recovered after issuance — see generate() — so nothing can
        # backfill it after the fact, and this module ships no migrations/
        # step to run at upgrade time). Bounded to exactly those records, so
        # the O(n)-PBKDF2 surface this finding closes shrinks to zero as
        # each pre-fix key is used once: a match here backfills its lookup
        # hash, after which every future call for that key takes the O(1)
        # path above. A key created after this fix always has
        # api_key_lookup_hash set and never reaches this branch.
        legacy_candidates = self.sudo().search([
            ("api_key_lookup_hash", "=", False),
            ("active", "=", True),
        ])
        for candidate in legacy_candidates:
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
