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
        # Security audit 2026-09, finding #2: the original patterns matched
        # only the exact names `pg_read_file`/`pg_ls_dir`, missing sibling
        # functions with the same filesystem reach —
        # pg_read_binary_file/pg_stat_file (read) and
        # pg_ls_waldir/pg_ls_logdir/pg_ls_tmpdir (list). Widened to match the
        # whole family; also subsumed by the blanket `\bpg_\w+\b` pattern
        # above, kept here for the specific documentation.
        r"\bpg_(read|stat|ls)_\w*\b",
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
        #
        # Security audit 2026-09, finding #2: pg_catalog sits on every role's
        # search_path implicitly, so an UNQUALIFIED read of a system relation
        # (`SELECT * FROM pg_stat_activity`, `pg_class`, `pg_proc`,
        # `pg_settings`, `pg_database`, `pg_tables`, ...) was never caught by
        # a guard that only matched the `pg_catalog.` prefix or a short,
        # explicit allowlist of names below. This one blanket pattern is the
        # real fix — it denies ANY bare identifier starting with `pg_`,
        # schema-qualified or not, known-by-name or not. The specific
        # patterns beneath it are now redundant in practice; they stay for
        # auditability/history (each documents *why* that particular table
        # was called out originally) and as a second line of defence should
        # this blanket pattern ever be narrowed.
        r"\bpg_\w+\b",
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
        # Security audit 2026-09, finding #4: PII/financial identifiers with
        # no credential shape, so the previous list waved every one of them
        # through to the LLM. Real Odoo field names, not guesses:
        r"\bvat\b",                    # res.partner — VAT / national tax id
        r"\bacc_number\b",             # res.partner.bank — raw bank account number
        r"\bsanitized_acc_number\b",   # res.partner.bank — normalised account number
        r"\biban\b",                   # IBAN, wherever a field is named it directly
        r"\bbank_account\b",           # generic bank-account-shaped field names
        r"\bnational_id\b",            # generic national-id-shaped field names
    ]
]

def is_denied_column(column_name: str) -> bool:
    """Whether a SELECT naming this column would be refused.

    Exists so ``get_schema`` can stop ADVERTISING columns this guard will
    refuse. Before it did, the agent was told ``account.move`` has an
    ``access_token`` column — perfectly true — wrote a query selecting it, and
    was refused. It has no way to know which of the names it was given are
    off-limits, so it cannot correct itself; it can only try a different
    column and be refused again.

    One list, asked twice: the guard decides, and the schema simply does not
    mention what the guard will not allow.
    """
    return any(pattern.search(column_name or "") for pattern in _DENIED_COLUMN_PATTERNS)


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

#: Security audit 2026-09, finding #2 (fail-open bug found while fixing it,
#: same mechanism, found and fixed in two passes — see both comments below):
#:
#: Pass 1 bug: this pattern originally matched double-quoted text as if it
#: were a second string-literal syntax, and blanked it the same way as a
#: single-quoted one. In Postgres, double quotes are IDENTIFIER quoting, not
#: a string literal — `"pg_authid"` is the table pg_authid, quoted, not a
#: string. Blanking it made every denylist check below blind to a quoted
#: identifier: `SELECT * FROM "pg_authid"` sailed straight through,
#: undetected by ANY pattern in this file, because the scan never saw the
#: text "pg_authid" at all.
#:
#: Pass 1 fix, pass 2 bug: simply dropping the double-quote alternative (so
#: only single-quoted text is matched/blanked) let an apostrophe INSIDE a
#: double-quoted identifier open a phantom single-quoted literal that runs to
#: the next real apostrophe anywhere later in the query — blanking
#: everything between them, live SQL included:
#: ``SELECT id AS "x'y", rolpassword FROM pg_authid WHERE z = 'a'`` — the
#: scanner sees an opening `'` inside `"x'y"` and doesn't close it until the
#: `'a'` near the end, blanking `pg_authid` out of the scan entirely, while
#: Postgres itself reads `"x'y"` as one quoted alias and executes the real
#: query underneath.
#:
#: The actual fix needs both properties at once: still match a whole
#: double-quoted span AS A UNIT (so an apostrophe inside it can never
#: desynchronize the single-quote scan), but keep its contents visible to
#: the denylist checks instead of blanking them, since those checks need to
#: see the real identifier name. ``_unmask_quoted_span`` below does that:
#: unwrap a double-quoted span to its bare contents, blank a single-quoted
#: one to ``''`` as before.
_QUOTED_SPAN_PATTERN: re.Pattern = re.compile(
    r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"", re.DOTALL
)


def _unmask_quoted_span(match: re.Match) -> str:
    span = match.group(0)
    if span.startswith('"'):
        # Identifier quoting: keep the name visible to the denylists below,
        # but the substitution happens once, over the ORIGINAL query text —
        # re.sub never rescans replacement text — so an apostrophe unwrapped
        # out of the identifier here cannot reopen a phantom literal either.
        return span[1:-1]
    return "''"


def mask_string_literals(query: str) -> str:
    """Blank out single-quoted string literals so keyword scanning reads
    code, not data; unwrap double-quoted identifiers to their bare name so
    the denylist checks below can still see it.

    ``WHERE partner_name ILIKE '%Drop Shipping%'`` is an ordinary accounting
    query. Scanning it raw refuses it for containing "drop".

    Both quote styles are matched by ONE pattern searched over the query
    text once, each whole span consumed atomically — never two independent
    passes, and never dropping one style from the pattern outright. Either
    of those looks like a smaller change but reopens the scanner to
    desynchronization from a quote character inside the other style's span.
    See the pattern's own comment for the two ways this already went wrong.
    """
    return _QUOTED_SPAN_PATTERN.sub(_unmask_quoted_span, query)


#: ``CROSS JOIN`` spelled out. It is what a comma-joined FROM list with no
#: WHERE clause means, written explicitly, and it is refused for the same reason.
_CROSS_JOIN_PATTERN: re.Pattern = re.compile(r"\bcross\s+join\b", re.IGNORECASE)

_FROM_PATTERN: re.Pattern = re.compile(r"\bfrom\b", re.IGNORECASE)

#: What can end a FROM list at its own nesting depth: a parenthesis (a
#: subquery or a function argument list), a comma (another table), or the
#: clause that follows the list. Only WHERE among those filters the product.
_FROM_LIST_TOKEN_PATTERN: re.Pattern = re.compile(
    r"[(),]|\b(?:where|group|having|order|limit|offset|union|intersect|except"
    r"|window|fetch|for)\b",
    re.IGNORECASE,
)

#: The refusal, worded for the model that has one job left: write the query
#: again. Identical in the three guards (see ``_unfiltered_comma_join``).
CARTESIAN_PRODUCT_REASON = (
    "Query multiplies tables together: a comma-separated FROM list with no "
    "WHERE clause, or a CROSS JOIN, pairs every row of each table with every "
    "row of the others and can take the database down. Write the join as "
    "JOIN ... ON <condition>, or add a WHERE clause that relates the tables."
)


def _unfiltered_comma_join(scannable: str) -> bool:
    """Whether any FROM list names two or more tables and never filters them.

    THE QUERY THIS REFUSES
        ``SELECT COUNT(*) FROM account_move_line a, account_move_line b,
        account_move_line c`` — three copies of a 675-row table, paired every
        way. Measured live: Odoo 18 ran it for ten seconds and answered
        304,821,217; the other three trees pinned their database until its own
        statement timeout killed the query thirty to sixty seconds later.
        Nothing in any guard had refused it, and a handful in parallel is an
        outage on the customer's own server, caused from inside the product.

    WHAT IT LEAVES ALONE
        A comma list WITH a WHERE clause (``FROM a, b WHERE a.id = b.a_id``) is
        the old spelling of an inner join and the desks write it; so is every
        explicit ``JOIN ... ON``. Neither is a product. Only a list the query
        never relates is refused — and ``CROSS JOIN``, which says so by name.

    HOW IT READS THE STATEMENT
        Not a parser. From each ``FROM``, walk forward counting commas at the
        list's own depth until the list ends: at a closing parenthesis (this
        FROM was inside a subquery, or a function such as ``EXTRACT(YEAR FROM
        d)``), at the next clause keyword, or at the end of the text. Commas
        inside parentheses belong to whatever the parentheses hold — an IN
        list, a subquery's own select list, ``USING (a, b)`` — and are never
        counted. A list ended by WHERE is filtered; one ended any other way,
        with a comma counted, is a product.

    THE THREE GUARDS AGREE. This function is repeated, line for line, in the
    platform's ``agent/tools/sql_guard.py`` and the Frappe app's
    ``agent_api/services/query_guard.py``, so a query refused here is refused
    there and a query allowed there is allowed here.
    """
    for opener in _FROM_PATTERN.finditer(scannable):
        depth = 0
        commas = 0
        ended_by_where = False
        for token in _FROM_LIST_TOKEN_PATTERN.finditer(scannable, opener.end()):
            text = token.group(0)
            if text == "(":
                depth += 1
            elif text == ")":
                if depth == 0:
                    break
                depth -= 1
            elif depth:
                continue
            elif text == ",":
                commas += 1
            else:
                ended_by_where = text.lower() == "where"
                break
        if commas and not ended_by_where:
            return True
    return False


def assert_query_is_read_only(query: str) -> None:
    """Refuse anything that is not a single, self-contained, read-only SELECT.

    Raises:
        ForbiddenQueryError: with a reason the agent can act on.
    """
    if not _SELECT_START_PATTERN.match(query):
        raise ForbiddenQueryError("Only SELECT queries are allowed for security reasons.")

    # A BACKSLASH IS REFUSED BEFORE THE LITERALS ARE MASKED.
    #
    #     ``mask_string_literals`` treats ``\\'`` as an escaped quote that keeps a
    #     string open — MariaDB's rule. PostgreSQL with
    #     ``standard_conforming_strings`` on (its default, and the dialect this
    #     app runs) does the opposite: ``'a\\'`` is the closed two-char string
    #     ``a\\`` and everything after it is live SQL. That divergence let a
    #     SINGLE-statement ``SELECT 'x\\' AS a, value FROM ir_config_parameter
    #     WHERE k='...`` mask its own ``FROM ir_config_parameter`` out of the
    #     scan below while Postgres executed the credential read underneath the
    #     LIMIT wrapper. No accounting SELECT needs a literal backslash (a quote
    #     inside a string is ``''``), so refusing it closes the whole divergence
    #     class — backslash escapes and ``E'...\\''`` escapes alike — without the
    #     guard having to reason about the dialect. Kept identical in the
    #     platform's ``agent/tools/sql_guard.py`` and the Frappe app's guard.
    if "\\" in query:
        raise ForbiddenQueryError(
            "Query must not contain a backslash (\\). Database engines disagree "
            "on whether it escapes a quote, which can hide a second statement or "
            "a forbidden table from this check. Write a literal quote inside a "
            "string as '' (two single quotes) and remove the backslash."
        )

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

    # Availability, not secrecy: a legitimate read that pairs every row with
    # every other row is refused here, before the database sees it, rather
    # than left for the statement timeout to kill thirty seconds later.
    if _CROSS_JOIN_PATTERN.search(scannable) or _unfiltered_comma_join(scannable):
        raise ForbiddenQueryError(CARTESIAN_PRODUCT_REASON)


# ─── Primary-table identification (security audit 2026-09, finding #1a) ────
#
# ``agent_api_service.validate_and_execute_query`` needs to know which table
# a caller-supplied SELECT reads from, so it can decide whether a company
# filter has to be appended before the query runs — ``allowed_company_ids``
# has no effect on this raw SQL, since it never goes through the ORM. This is
# intentionally NOT a SQL parser: it is the same lightweight, regex-based
# identification style the denylist checks above already use, extended to
# name a table instead of just detecting one. Its accuracy claims are
# deliberately narrow — see each function's docstring for exactly what it
# does and does not resolve.

@dataclass(frozen=True, slots=True)
class GuardedResult:
    """What ``agent_api_service.py`` hands back to the controller."""

    columns: list[str]
    data: list[dict]
    row_count: int
    truncated: bool
    max_rows: int
