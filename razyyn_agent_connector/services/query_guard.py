# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""The SQL guard this app must enforce, per ``razyyn/docs/erp/SQL_GUARD_CONTRACT.md``.

WHY THIS FILE HAS NO ``import odoo``
    Every hostile query this refuses was written by a language model, not by
    this app's own users — the query text can carry an instruction planted in
    a document a stranger emailed the customer. That makes this module the
    actual security boundary the agent depends on, not a formality, and the
    thing a security boundary needs most is to be checkable on its own: no
    live database, no installed Odoo, just the corpus in and a verdict out.
    ``tests/test_query_guard.py`` runs this file directly for that reason.

    ``services/agent_api_service.py`` is the thin layer that calls this from
    inside a real Odoo transaction.

THIS IS A PORT, NOT AN INDEPENDENT DESIGN
    The rules, patterns and reasoning below mirror
    ``razyyn-frappe-15/accountant_agent/agent_api/services/agent_api_service.py``
    line for line where Postgres and Odoo's naming allow it, so the two apps
    the agent talks to enforce the same guarantee. Where they differ — Odoo's
    denied tables are Odoo's own credential tables, not Frappe's — the
    difference is called out inline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class ForbiddenQueryError(Exception):
    """Raised when a SQL query is not a single, read-only, safe SELECT.

    Carries the reason the agent can act on — "Query contains forbidden
    keyword: update" lets the model correct itself; "Forbidden" does not, and
    the model just retries the same query forever.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# ─── Query Safety Policy ────────────────────────────────────────────────────
#
# Everything this endpoint executes was written by a language model, and that
# model's context can contain text supplied by whoever sent the customer a
# document. The guards below therefore assume a hostile query author, not a
# careless one — see SQL_GUARD_CONTRACT.md requirements 1-7.

#: Requirement 1 & 2: statements that modify data or schema, or stack a second
#: statement. Matched against a query whose string literals have been masked
#: first (requirement 3) — otherwise an invoice whose description contains the
#: word "update" is refused, and the model retries the identical query forever.
_FORBIDDEN_SQL_PATTERNS: list[re.Pattern] = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\binsert\b",
        r"\bupdate\b",
        r"\bdelete\b",
        r"\bdrop\b",
        r"\balter\b",
        r"\bcreate\b",
        r"\btruncate\b",
        r"\breplace\s+into\b",
        r"\brename\b",
        r"\bgrant\b",
        r"\brevoke\b",
        r"\bexecute\b",
        r"\bcall\b",
        r"\bcopy\b",              # Postgres COPY can read or write the filesystem
        r"\blo_import\b",
        r"\blo_export\b",
        r"\bpg_read_file\b",
        r"\bpg_ls_dir\b",
        r"\bdblink\b",            # cross-database calls out of a supposedly local read
        r"\bload_file\b",         # MySQL file read — denied even on Postgres; a
        r"\boutfile\b",           # guard scoped to "the dialect we ship on today"
        r"\bdumpfile\b",          # is not a guard, per the contract's own warning
        r"\binto\s+outfile\b",    # about denying only one engine family's names
        r"\bset\s+session\b",
        r"\bset\s+global\b",
    ]
]

_SELECT_START_PATTERN: re.Pattern = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

#: Requirement 4: tables that hold credentials, sessions, or engine internals.
#: None of them is an accounting record, so refusing them costs the product
#: nothing, while allowing them turns one prompt injection in an uploaded PDF
#: into the exfiltration of every stored secret on the customer's database.
#:
#: Both database families are denied regardless of which one this app actually
#: runs on (requirement 4's own instruction) — Postgres is the only dialect
#: Odoo ships on today, but a guard that only knows today's dialect is not one.
_DENIED_IDENTIFIER_PATTERNS: list[re.Pattern] = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        # Postgres engine catalogues. information_schema is handled separately
        # below — the audit agent legitimately enumerates tables and columns
        # through it, so a blanket refusal here would break schema discovery.
        r"\bpg_catalog\b",
        r"\bpg_shadow\b",
        r"\bpg_authid\b",
        r"\bpg_user\b",
        r"\bpg_roles\b",
        # MariaDB/MySQL catalogues — denied even though this app runs on
        # Postgres, per the contract's explicit instruction.
        r"\bperformance_schema\b",
        r"\bmysql\s*\.",
        # This module's own credential table.
        r"\brazyyn_agent_settings\b",   # holds this agent's own API key hash
        # Odoo's own credential and integration-secret tables.
        r"\bres_users_apikeys\b",       # hashed API keys issued to Odoo users
        r"\bres_users_apikeys_description\b",
        r"\bir_config_parameter\b",     # database.secret and other site secrets
        r"\bir_mail_server\b",          # outgoing SMTP credentials
        r"\bfetchmail_server\b",        # incoming mail server credentials
        r"\bauth_oauth_provider\b",     # OAuth client secrets
        r"\biap_account\b",             # Odoo IAP service tokens
        r"\bironing_signup_token\b",    # legacy signup tokens (older Odoo trees)
        r"\bres_device_log\b",          # session/device identifiers
        r"\bir_sessions\b",
    ]
]

#: Requirement 5: column names that carry secrets wherever they appear.
#: ``res_users`` is otherwise a legitimate read — an accountant asks who
#: posted a journal entry — so the block is on the sensitive columns, not the
#: whole table.
_DENIED_COLUMN_PATTERNS: list[re.Pattern] = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bpassword\b",
        r"\bpassword_crypt\b",
        r"\btotp_secret\b",
        r"\bapi_key\b",
        r"\bclient_secret\b",
        r"\baccess_token\b",
        r"\brefresh_token\b",
        r"\bsignup_token\b",
        r"\bdatabase_secret\b",
    ]
]

#: Requirement 7: rows returned to the agent. Matches the contract published
#: in the ``ODOO`` adapter's SQL guidance block.
DEFAULT_MAX_RESULT_ROWS: int = 500

#: Requirement 7: seconds of server-side execution a single query may consume.
#: The agent's own HTTP timeout only releases the caller — the database keeps
#: running an abandoned query and keeps contending with the customer's own
#: postings. Only the database can stop it, so this is enforced with
#: ``SET LOCAL statement_timeout`` in the same transaction (see
#: ``agent_api_service.py``), not checked after the fact here.
DEFAULT_QUERY_TIMEOUT_SECONDS: int = 30

#: Requirement 6: the only two information_schema views the agent has a reason
#: to read. The audit agent discovers the customer's ledger tables and their
#: columns through them — schema shape, never data, never privileges, never a
#: live session list.
_ALLOWED_INFORMATION_SCHEMA_VIEWS: frozenset[str] = frozenset({"tables", "columns"})

_INFORMATION_SCHEMA_PATTERN: re.Pattern = re.compile(
    r"\binformation_schema\b(?:\s*\.\s*([a-zA-Z0-9_]+))?", re.IGNORECASE
)

_STRING_LITERAL_PATTERN: re.Pattern = re.compile(
    r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"", re.DOTALL
)


def mask_string_literals(query: str) -> str:
    """Blank out quoted literals so keyword scanning reads code, not data.

    ``WHERE partner_name ILIKE '%Drop Shipping%'`` is an ordinary accounting
    query. Scanning it raw refuses it for containing "drop".
    """
    return _STRING_LITERAL_PATTERN.sub("''", query)


def assert_query_is_read_only(query: str) -> None:
    """Refuse anything that is not a single, self-contained, read-only SELECT.

    Raises:
        ForbiddenQueryError: with a reason the agent can act on.
    """
    if not _SELECT_START_PATTERN.match(query):
        raise ForbiddenQueryError("Only SELECT queries are allowed for security reasons.")

    scannable = mask_string_literals(query)

    # Stacked statements. psycopg2 executes every statement in a multi-statement
    # string, so the SELECT-must-come-first check alone waves through
    # `SELECT 1; DELETE FROM res_partner`.
    if ";" in scannable.rstrip().rstrip(";"):
        raise ForbiddenQueryError("Only a single statement may be executed per request.")

    for pattern in _FORBIDDEN_SQL_PATTERNS:
        match = pattern.search(scannable)
        if match:
            raise ForbiddenQueryError(f"Query contains forbidden keyword: {match.group(0)}")

    for pattern in _DENIED_IDENTIFIER_PATTERNS:
        if pattern.search(scannable):
            raise ForbiddenQueryError(
                "This query reads a system or credential table, which is not "
                "permitted. Only business records are available."
            )

    for pattern in _DENIED_COLUMN_PATTERNS:
        if pattern.search(scannable):
            raise ForbiddenQueryError("This query reads a credential column, which is not permitted.")

    for match in _INFORMATION_SCHEMA_PATTERN.finditer(scannable):
        view = (match.group(1) or "").lower()
        if view not in _ALLOWED_INFORMATION_SCHEMA_VIEWS:
            raise ForbiddenQueryError(
                "Only information_schema.tables and information_schema.columns may be read."
            )


@dataclass(frozen=True, slots=True)
class GuardedResult:
    """What ``agent_api_service.py`` hands back to the controller."""

    columns: list[str]
    data: list[dict]
    row_count: int
    truncated: bool
    max_rows: int
