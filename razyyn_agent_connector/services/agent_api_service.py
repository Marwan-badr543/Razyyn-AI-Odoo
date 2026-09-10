# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Service layer — everything the controller needs, with an Odoo environment
in hand. Pure business logic; no HTTP status codes or request objects here
(``controllers/main.py`` owns that translation), matching the three-layer
convention the razyyn agent's own backend follows (project_rules.md).

Guard enforcement is delegated to ``query_guard.py`` — nothing about SQL
safety is re-decided in this file.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta

from odoo import fields as odoo_fields

from .query_guard import (  # noqa: F401 (ForbiddenQueryError/assert_query_is_read_only re-exported)
    ForbiddenQueryError,
    assert_query_is_read_only,
    extract_primary_table,
    select_list_exposes_company_id,
)

DEFAULT_MAX_RESULT_ROWS = 500
DEFAULT_QUERY_TIMEOUT_SECONDS = 30

#: Layout-only field types with no data — excluded from schema summaries, the
#: same reasoning the Frappe app applies to Section/Column Break.
_IGNORED_FIELD_TYPES = {"separator"}

MAX_GENERATED_FILE_BYTES = 25 * 1024 * 1024
GENERATED_FILE_RETENTION_HOURS = 3


class MissingParameterError(Exception):
    def __init__(self, parameter_name: str) -> None:
        self.parameter_name = parameter_name
        super().__init__(f"Missing required parameter: {parameter_name}")


class ResourceNotFoundError(Exception):
    def __init__(self, resource_type: str, identifier: str) -> None:
        self.resource_type = resource_type
        self.identifier = identifier
        super().__init__(f"{resource_type} '{identifier}' not found.")


class QueryExecutionError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"SQL Execution Error: {detail}")


class FileTooLargeError(Exception):
    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(f"File of {size_bytes} bytes exceeds the {limit_bytes} byte limit.")


class InvalidPayloadFormatError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


# ─── Authentication ──────────────────────────────────────────────────────────


def authenticate_by_api_key(env, api_key):
    """Return the ``razyyn.agent.settings`` record for *api_key*, or ``None``.

    Security audit 2026-09, finding #1b: intentionally left on ``.sudo()``.
    This call establishes identity — it is what tells the caller *which*
    company's connection it holds — so there is no company (or user) to scope
    it to yet; every other ``.sudo()`` in this file runs after this one has
    already resolved an ``agent_settings`` record and is scoped from there.
    """
    return env["razyyn.agent.settings"].sudo().authenticate(api_key)


# ─── SQL Query Execution ─────────────────────────────────────────────────────


def _table_has_company_id_column(cr, table_name: str) -> bool:
    """Whether *table_name* (already vetted by
    ``query_guard.extract_primary_table`` — public schema, matches a plain
    identifier) carries a ``company_id`` column, per Odoo's own multi-company
    convention.

    This is its own, ordinary parameterized query against
    ``information_schema.columns`` — a completely separate ``cr.execute()``
    call from the one that runs the caller's SQL below, so it is unaffected
    by (and does not reintroduce) the no-parameters constraint documented on
    that call.
    """
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = 'company_id'",
        (table_name,),
    )
    return cr.fetchone() is not None


def validate_and_execute_query(env, sql_query: str, allowed_company_ids,
                                max_rows: int = DEFAULT_MAX_RESULT_ROWS,
                                timeout_seconds: int = DEFAULT_QUERY_TIMEOUT_SECONDS) -> dict:
    """Validate a SQL query for read-only safety, then execute it under limits.

    *allowed_company_ids* are the caller's connection's own company id(s) —
    server-controlled integers (``agent_settings.company_id.id``), never
    agent-supplied text. See the company-scoping block below (security audit
    2026-09, finding #1a): this raw SQL never goes through the ORM, so
    ``allowed_company_ids`` set as an *env context* has no effect on it
    whatsoever — the only place a company boundary can be enforced for this
    query is the SQL text itself.

    Raises:
        MissingParameterError, ForbiddenQueryError (from query_guard),
        QueryExecutionError.
    """
    if not sql_query:
        raise MissingParameterError("sql_query")

    clean_query = sql_query.strip().rstrip(";")
    assert_query_is_read_only(clean_query)  # raises ForbiddenQueryError

    # int() coerces or raises — this can never carry agent-supplied text,
    # even if a caller of this function someday passes something looser than
    # a list of ints.
    company_ids = [int(cid) for cid in allowed_company_ids]

    cr = env.cr
    try:
        # A failing agent query (a bad column name — expected, and deliberately
        # forwarded below so the model can correct itself) would otherwise
        # abort `cr`'s whole transaction: every statement after it on this
        # cursor — including the response this function is building — would
        # raise InFailedSqlTransaction instead of returning the error the
        # agent needs to retry. A savepoint contains that to this query alone.
        with cr.savepoint():
            # Requirement 7 (part 1): server-side statement timeout. SET LOCAL
            # is savepoint/transaction-scoped, so it cannot leak onto whatever
            # request reuses this connection next.
            cr.execute("SET LOCAL statement_timeout = %s", (int(timeout_seconds) * 1000,))

            # ─── Company scoping (security audit 2026-09, finding #1a) ──────
            # Best-effort identification of the query's primary FROM table —
            # see query_guard.extract_primary_table for exactly what this
            # does and does not resolve (a leading CTE, a derived FROM table,
            # or any table past the first one in a UNION/JOIN is a known,
            # documented gap: such a query runs WITHOUT a company filter,
            # same as before this fix).
            primary_table = extract_primary_table(clean_query)
            scoped_query = clean_query
            if primary_table and _table_has_company_id_column(cr, primary_table):
                # The table is multi-company. The only place a filter can be
                # appended is an OUTER wrapper around the caller's own query,
                # which can only filter on a column that query's own SELECT
                # list actually projects — rewriting the caller's SELECT list
                # itself is out of scope here. A query that hides company_id
                # (an explicit column list, or an aggregate that doesn't
                # select/group by it) is therefore refused rather than run
                # unscoped or silently miss rows it can't filter.
                if not select_list_exposes_company_id(clean_query):
                    raise ForbiddenQueryError(
                        f"'{primary_table}' is a multi-company table. Include "
                        "company_id in the SELECT list (use SELECT *, or add "
                        "company_id — and GROUP BY company_id for an "
                        "aggregate) so this query can be scoped to your "
                        "connection's company."
                    )
                # NULL company_id means "shared across every company" under
                # Odoo's own multi-company convention — it is the normal
                # state for most res.partner rows, many product.template
                # rows, etc. — and mirrors the OR clause Odoo's own built-in
                # multi-company record rules use
                # (['|', ('company_id', '=', False), ('company_id', 'in', ids)]).
                # Omitting it here would silently hide every shared record
                # rather than scope the query, which reads as "the agent
                # can't see its own customers", not as a security fix.
                #
                # An empty allowed_company_ids (should not happen —
                # company_id is a required field on razyyn.agent.settings —
                # but handled defensively) denies everything rather than
                # falling back to the NULL-only clause above: a connection
                # with no resolvable company gets no rows, not "every shared
                # record on the database".
                if company_ids:
                    ids_sql = ", ".join(str(cid) for cid in company_ids)
                    company_predicate = f"(company_id IS NULL OR company_id IN ({ids_sql}))"
                else:
                    company_predicate = "FALSE"
                scoped_query = (
                    f"SELECT * FROM (\n{clean_query}\n) AS razyyn_agent_company_scoped\n"
                    f"WHERE {company_predicate}"
                )

            # Requirement 7 (part 2): row cap, enforced by the database via
            # LIMIT on a wrapping query rather than sliced in Python after the
            # fact — a query that would return ten years of ledger rows never
            # materialises them past max_rows + 1. The wrapper is valid
            # whether or not the inner query is itself a WITH ... SELECT.
            #
            # The inner query is on its own line, between the wrapper's own
            # open and close parens. Without that, a query ending in a line
            # comment (`SELECT ... -- note`) would comment out everything
            # after it on the same line — including this wrapper's closing
            # `) AS ... LIMIT %s` — and either break or, worse, silently drop
            # the row cap.
            #
            # The cap (and, above, the company filter) is inlined as an
            # integer rather than passed as a psycopg2 parameter, and the
            # execute below is called with NO parameter tuple, on purpose.
            # Passing *any* parameters makes psycopg2 %-interpolate the
            # ENTIRE statement — the agent's own SQL included — so an
            # ordinary query carrying a percent sign (`LIKE '%acme%'`, or
            # `amount % 2`) has its `%` read as a placeholder and dies with
            # "tuple index out of range". max_rows and company_ids are both
            # server-controlled ints, never agent text, so inlining them is
            # safe and keeps psycopg2 out of the agent's SQL entirely.
            capped = f"SELECT * FROM (\n{scoped_query}\n) AS razyyn_agent_capped LIMIT {int(max_rows) + 1}"
            cr.execute(capped)
            rows = cr.dictfetchall()
    except ForbiddenQueryError:
        raise
    except Exception as exc:
        raise QueryExecutionError(str(exc)) from exc

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]

    columns = list(rows[0].keys()) if rows else []
    return {
        "success": True,
        "columns": columns,
        "data": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "max_rows": max_rows,
    }


# ─── Schema Summary ──────────────────────────────────────────────────────────


def build_schema_summary(env, model_name: str, agent_settings=None) -> dict:
    """Field summary for one Odoo model, shaped for LLM consumption — the
    Odoo counterpart of the Frappe app's ``build_doctype_schema_summary``.

    Security audit 2026-09, finding #1b: ``.with_company()`` is applied below
    for consistency with the other ORM call sites in this file, but it is a
    documented no-op here — ``ir.model``/``ir.model.fields`` are global Odoo
    metadata with no ``company_id`` field at all, so there is no per-company
    data for it to scope. Nothing this function reads varies by company.

    Raises:
        MissingParameterError, ResourceNotFoundError.
    """
    if not model_name:
        raise MissingParameterError("model")

    if model_name not in env:
        raise ResourceNotFoundError("Model", model_name)

    ir_model = env["ir.model"].sudo()
    ir_model_fields = env["ir.model.fields"].sudo()
    if agent_settings is not None:
        ir_model = ir_model.with_company(agent_settings.company_id)
        ir_model_fields = ir_model_fields.with_company(agent_settings.company_id)

    model_record = ir_model.search([("model", "=", model_name)], limit=1)
    if not model_record:
        raise ResourceNotFoundError("Model", model_name)

    field_records = ir_model_fields.search([("model", "=", model_name)])

    fields_summary = ["id (integer, primary key)"]
    for field in field_records:
        if field.ttype in _IGNORED_FIELD_TYPES:
            continue
        info = f"{field.name} ({field.ttype})"
        if field.field_description:
            info += f" - {field.field_description}"
        if field.required:
            info += " [Required]"
        if field.ttype in ("many2one", "one2many", "many2many") and field.relation:
            info += f" -> relation {field.relation}"
        if field.ttype == "selection" and field.selection:
            info += f" [Options: {field.selection}]"
        fields_summary.append(info)

    return {
        "success": True,
        "model": model_name,
        "table_name": model_name.replace(".", "_"),
        "fields": fields_summary,
    }


# ─── Session ownership (IDOR guard) ──────────────────────────────────────────


def get_or_create_session(env, session_id: str, agent_settings):
    # Security audit 2026-09, finding #1b: .with_company() is applied for
    # consistency/defense-in-depth. It is also a documented no-op today —
    # razyyn.agent.chat.session carries no company_id field, so there is
    # nothing here for it to scope. The real boundary on this method is the
    # ownership check a few lines down (agent_settings_id == the caller's own
    # connection), which is unrelated to company and unaffected by this.
    Session = env["razyyn.agent.chat.session"].sudo().with_company(agent_settings.company_id)
    session = Session.search([("session_id", "=", session_id)], limit=1)
    if session:
        if session.agent_settings_id.id != agent_settings.id:
            # Exists, but not this caller's — same "not found" answer as a
            # session that never existed, so this endpoint cannot be used to
            # enumerate another connection's session ids by response alone.
            raise ResourceNotFoundError("Chat session", session_id)
        session.touch()
        return session
    return Session.create({"session_id": session_id, "agent_settings_id": agent_settings.id})


# ─── Clarification requests ──────────────────────────────────────────────────


def parse_questions_payload(questions_raw) -> list:
    if isinstance(questions_raw, list):
        return questions_raw
    try:
        parsed = json.loads(questions_raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidPayloadFormatError(f"Invalid questions format. Must be a JSON array. Error: {exc}") from exc
    if not isinstance(parsed, list):
        raise InvalidPayloadFormatError("questions must be a list / JSON array.")
    return parsed


def process_clarification_request(env, session_id: str, questions_raw, agent_settings) -> dict:
    if not session_id:
        raise MissingParameterError("session_id")
    if not questions_raw:
        raise MissingParameterError("questions")

    parsed_questions = parse_questions_payload(questions_raw)
    session = get_or_create_session(env, session_id, agent_settings)

    content = json.dumps({"type": "clarification", "questions": parsed_questions}, ensure_ascii=False)
    # Security audit 2026-09, finding #1b: .with_company() applied for
    # consistency with the other write sites in this file — see
    # get_or_create_session's comment on why this is a documented no-op
    # today (razyyn.agent.chat.message carries no company_id field either).
    env["razyyn.agent.chat.message"].sudo().with_company(agent_settings.company_id).create({
        "session_id": session.id,
        "sender": "agent",
        "kind": "clarification",
        "content": content,
    })

    # Odoo's realtime channel, the counterpart of frappe.publish_realtime —
    # notifies any UI subscribed to this session's bus channel.
    env["bus.bus"]._sendone(
        f"razyyn_agent_session_{session_id}",
        "razyyn_agent_clarification_requested",
        {"session_id": session_id, "questions": parsed_questions},
    )

    return {"success": True, "message": "Clarification request saved and broadcasted successfully."}


# ─── Generated file upload ───────────────────────────────────────────────────


def save_generated_file(env, session_id: str, filename: str, content: bytes, agent_settings) -> dict:
    if not session_id:
        raise MissingParameterError("session_id")
    if len(content) > MAX_GENERATED_FILE_BYTES:
        raise FileTooLargeError(len(content), MAX_GENERATED_FILE_BYTES)

    session = get_or_create_session(env, session_id, agent_settings)

    # Security audit 2026-09, finding #1b: .with_company() applied on both
    # writes below for consistency — a documented no-op today (see
    # get_or_create_session's comment): neither ir.attachment as used here
    # nor razyyn.agent.chat.message carries a company_id field for it to
    # scope.
    attachment = env["ir.attachment"].sudo().with_company(agent_settings.company_id).create({
        "name": filename,
        # `datas` is base64 IN, not raw bytes — ir.attachment's own inverse
        # b64-decodes whatever it is given; raw bytes here corrupt the file
        # or raise, they are not encoded a second time for you.
        "datas": base64.b64encode(content),
        "res_model": "razyyn.agent.chat.session",
        "res_id": session.id,
        "public": False,
    })
    env["razyyn.agent.chat.message"].sudo().with_company(agent_settings.company_id).create({
        "session_id": session.id,
        "sender": "agent",
        "kind": "generated_file",
        "content": json.dumps({"filename": filename}),
        "attachment_id": attachment.id,
    })

    return {"success": True, "attachment_id": attachment.id, "filename": filename}


def cleanup_old_files(env) -> int:
    """Hourly sweep of expired agent-generated reports — the Odoo counterpart
    of the Frappe app's ``cleanup_old_files``. Scoped to attachments this
    module itself created (``res_model`` is this module's own session model),
    so nothing an accountant uploaded elsewhere in Odoo is ever at risk here.
    """
    cutoff = odoo_fields.Datetime.now() - timedelta(hours=GENERATED_FILE_RETENTION_HOURS)
    expired = env["ir.attachment"].sudo().search([
        ("res_model", "=", "razyyn.agent.chat.session"),
        ("create_date", "<", cutoff),
    ], limit=500)
    count = len(expired)
    expired.unlink()
    return count


# ─── Messaging (honest stub) ─────────────────────────────────────────────────
#
# The Frappe app's messaging service wraps Gmail/WhatsApp/Telegram business
# accounts configured per customer (z_plan/project_next_features.md #3).
# Porting that whole provider layer is out of scope for this connector's first
# version — see README.md — so these two endpoints answer honestly rather
# than pretending to send: get_messaging_config reports no channels
# configured, and send_message therefore always refuses with a reason the
# agent can relay ("email is not switched on here"), never a silent no-op
# reported as success.


class ChannelNotConfiguredError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def get_messaging_config(env) -> dict:
    return {"success": True, "channels": []}


def send_message(env, **_kwargs) -> dict:
    raise ChannelNotConfiguredError(
        "No messaging channel is configured on this site yet."
    )
