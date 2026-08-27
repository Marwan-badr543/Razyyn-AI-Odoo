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

from .query_guard import ForbiddenQueryError, assert_query_is_read_only  # noqa: F401 (re-exported)

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
    """Return the ``razyyn.agent.settings`` record for *api_key*, or ``None``."""
    return env["razyyn.agent.settings"].sudo().authenticate(api_key)


# ─── SQL Query Execution ─────────────────────────────────────────────────────


def validate_and_execute_query(env, sql_query: str, max_rows: int = DEFAULT_MAX_RESULT_ROWS,
                                timeout_seconds: int = DEFAULT_QUERY_TIMEOUT_SECONDS) -> dict:
    """Validate a SQL query for read-only safety, then execute it under limits.

    Raises:
        MissingParameterError, ForbiddenQueryError (from query_guard),
        QueryExecutionError.
    """
    if not sql_query:
        raise MissingParameterError("sql_query")

    clean_query = sql_query.strip().rstrip(";")
    assert_query_is_read_only(clean_query)  # raises ForbiddenQueryError

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
            capped = f"SELECT * FROM (\n{clean_query}\n) AS razyyn_agent_capped LIMIT %s"
            cr.execute(capped, (max_rows + 1,))
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


def build_schema_summary(env, model_name: str) -> dict:
    """Field summary for one Odoo model, shaped for LLM consumption — the
    Odoo counterpart of the Frappe app's ``build_doctype_schema_summary``.

    Raises:
        MissingParameterError, ResourceNotFoundError.
    """
    if not model_name:
        raise MissingParameterError("model")

    if model_name not in env:
        raise ResourceNotFoundError("Model", model_name)

    model_record = env["ir.model"].sudo().search([("model", "=", model_name)], limit=1)
    if not model_record:
        raise ResourceNotFoundError("Model", model_name)

    field_records = env["ir.model.fields"].sudo().search([("model", "=", model_name)])

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
    Session = env["razyyn.agent.chat.session"].sudo()
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
    env["razyyn.agent.chat.message"].sudo().create({
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

    attachment = env["ir.attachment"].sudo().create({
        "name": filename,
        # `datas` is base64 IN, not raw bytes — ir.attachment's own inverse
        # b64-decodes whatever it is given; raw bytes here corrupt the file
        # or raise, they are not encoded a second time for you.
        "datas": base64.b64encode(content),
        "res_model": "razyyn.agent.chat.session",
        "res_id": session.id,
        "public": False,
    })
    env["razyyn.agent.chat.message"].sudo().create({
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
