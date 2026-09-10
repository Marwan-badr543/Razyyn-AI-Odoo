# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Controller (Router) Layer — Agent API.

HTTP parsing, auth-key extraction, and mapping domain exceptions to HTTP
status codes. No ORM calls and no business validation here — see
``services/agent_api_service.py`` — matching the three-layer split the
razyyn agent's own backend follows.

WHY PARAMS ARE PARSED BY HAND INSTEAD OF READ FROM ``**kwargs``
    The agent posts a JSON body (``httpx.AsyncClient.post(url, json=payload)``
    in ``agent/tools/tools.py`` and ``agent/tools/messaging.py`` on the
    ``razyyn`` side) for every route except ``upload_file``, which posts
    multipart form data. Odoo's ``type="http"`` dispatcher builds the
    ``**kwargs`` a route method receives from the query string and
    form-encoded/multipart bodies (werkzeug's ``request.form``) — never from a
    raw ``application/json`` body. A JSON POST therefore arrives with an empty
    ``kwargs`` here, which is silent: the first symptom is "missing API key",
    not a shape error, because the key is *also* in that same unread body.
    ``type="json"`` is not the fix either — that expects a JSON-RPC 2.0
    envelope in and produces one out, which breaks the reply shape below.
    ``_params()`` reads the raw body itself when the content type says JSON,
    and falls back to ``request.params`` (query + form) otherwise, so both of
    the agent's own request shapes are actually read.

WHY EVERY SUCCESSFUL REPLY IS WRAPPED IN ``{"message": ...}``
    ``razyyn/agent/tools/tools.py`` and ``document_generator.py`` unwrap every
    ERP reply with ``response.json().get("message", {})`` — a convention this
    reply shape has to honour, or the agent's own already-tested parsing
    silently reads an empty dict instead of real data. That parsing is a
    Frappe-ism that leaked out of the port and ideally becomes adapter-agnostic
    the way ``agent/tools/messaging.py``'s own reply parsing already is; until
    that lands, this app conforms to the wire shape the caller actually
    expects rather than the one that would be cleanest in isolation. Errors
    are returned bare (``{"error": "..."}"``), matching the same caller's
    error-path parsing.
"""

import json

from odoo import http
from odoo.http import request

from ..services import agent_api_service as svc
from ..services.query_guard import ForbiddenQueryError


def _params():
    """The request's own parameters, JSON body or form/query — see module
    docstring for why this can't just be the route method's ``**kwargs``."""
    if (request.httprequest.mimetype or "").endswith("json"):
        try:
            body = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
    return dict(request.params)


def _extract_api_key(params):
    return params.get("api_key") or request.httprequest.headers.get("X-API-Key")


def _env_scoped_to(agent_settings):
    """An environment run as the platform's own service account (uid 1).

    Uid 1 rather than the calling accountant's own user: the agent is a
    machine caller authenticated by API key, not a logged-in person, and
    ``razyyn.agent.settings`` carries no Odoo user of its own to run as — the
    same "reading needs no grant" position the Frappe app documents in
    ``agent_api_repository.py``.

    CORRECTION (security audit 2026-09, finding #1b) — the paragraph this
    replaced claimed ``allowed_company_ids`` "bounds every ORM read this
    environment makes to the company this API key was issued for". That was
    wrong: uid 1 is Odoo's ``SUPERUSER_ID``, and Odoo's own ``Environment``
    factory forces ``env.su = True`` whenever ``uid == SUPERUSER_ID``
    (verify against the target Odoo tree's ``odoo/api.py`` if this behaviour
    ever needs re-confirming) — the same effect as calling ``.sudo()``,
    regardless of what this call's own ``context`` carries. ir.rule record
    rules — the mechanism ``allowed_company_ids`` actually feeds — never run
    for a superuser environment at all. So this context has never bounded
    anything; it is left in place only because removing it changes nothing
    either way, and future code depending on it here would be a bug.
    Everywhere company scoping needs to be real, it is now enforced
    explicitly instead of leaned on implicitly:
      - the raw-SQL path (``execute_query`` below) is scoped at the SQL
        text level — see ``agent_api_service.validate_and_execute_query``;
      - the ORM call sites (``get_schema``, session/message/attachment
        writes) are scoped by ``.with_company()`` per call, which is itself
        a documented no-op today because none of the models involved
        (``razyyn.agent.chat.session/.message``, ``ir.model``,
        ``ir.model.fields``, and ``ir.attachment`` as used here) carry a
        ``company_id`` field — see each call site's own comment in
        ``agent_api_service.py``;
      - ``get_or_create_session``'s IDOR ownership check
        (``agent_settings_id`` match) is what actually protects
        session/message/attachment access, and was never dependent on this
        context to begin with.
    Making ORM reads genuinely company-rule-enforced (rather than
    structurally a no-op today) would mean this environment stops being a
    superuser one — e.g. a real, low-privileged "Agent" res.users account
    per company with its own ir.rule — which is a larger change than this
    fix and is flagged here as a residual architecture gap, not implemented.
    """
    return request.env(user=1, context={"allowed_company_ids": [agent_settings.company_id.id]})


def _error(status_code, message):
    return request.make_json_response({"error": message}, status=status_code)


def _ok(payload):
    return request.make_json_response({"message": payload})


def _authenticated(params):
    """Resolve the API key once, or short-circuit with 401/403."""
    api_key = _extract_api_key(params)
    if not api_key:
        return None, _error(401, "Missing API Key. Authentication required.")
    agent_settings = svc.authenticate_by_api_key(request.env, api_key)
    if not agent_settings:
        return None, _error(403, "Invalid API Key. Authentication failed.")
    return agent_settings, None


class RazyynAgentApiController(http.Controller):

    @http.route("/agent_api/execute_query", type="http", auth="public",
                methods=["POST"], csrf=False)
    def execute_query(self, **_kwargs):
        params = _params()
        agent_settings, failure = _authenticated(params)
        if failure:
            return failure

        sql_query = params.get("sql_query")
        env = _env_scoped_to(agent_settings)
        # Security audit 2026-09, finding #1a: the agent's own SQL is now
        # scoped to this connection's company at the SQL text level itself —
        # not by the env's allowed_company_ids context, which never applied
        # to raw SQL and (per _env_scoped_to's docstring correction above)
        # would not have enforced anything via the ORM either, since this
        # env is a superuser one. See
        # agent_api_service.validate_and_execute_query for the wrapping and
        # its documented gaps (CTEs, multi-table UNIONs/JOINs, aggregates
        # that omit company_id).
        try:
            result = svc.validate_and_execute_query(
                env, sql_query, [agent_settings.company_id.id],
            )
        except svc.MissingParameterError:
            return _error(400, "Missing SQL query.")
        except ForbiddenQueryError as exc:
            return _error(400, exc.reason)
        except svc.QueryExecutionError as exc:
            # Forwarded by design: this endpoint's only caller is the agent,
            # never an end user, so a bad-column-name or timeout message
            # helps it correct its own SQL. The agent's own persona rules
            # keep raw errors off the customer's screen (project_rules.md).
            return _error(500, str(exc))

        return _ok(result)

    @http.route("/agent_api/get_schema", type="http", auth="public",
                methods=["POST"], csrf=False)
    def get_schema(self, **_kwargs):
        params = _params()
        agent_settings, failure = _authenticated(params)
        if failure:
            return failure

        model_name = params.get("model") or params.get("doctype")
        env = _env_scoped_to(agent_settings)
        try:
            result = svc.build_schema_summary(env, model_name, agent_settings)
        except svc.MissingParameterError:
            return _error(400, "Missing model parameter.")
        except svc.ResourceNotFoundError as exc:
            return _error(404, str(exc))

        return _ok(result)

    @http.route("/agent_api/request_clarification", type="http", auth="public",
                methods=["POST"], csrf=False)
    def request_clarification(self, **_kwargs):
        params = _params()
        agent_settings, failure = _authenticated(params)
        if failure:
            return failure

        session_id = params.get("session_id")
        questions = params.get("questions")
        env = _env_scoped_to(agent_settings)
        try:
            result = svc.process_clarification_request(env, session_id, questions, agent_settings)
        except svc.MissingParameterError as exc:
            return _error(400, f"Missing {exc.parameter_name} parameter.")
        except svc.ResourceNotFoundError as exc:
            return _error(404, str(exc))
        except svc.InvalidPayloadFormatError as exc:
            return _error(400, exc.detail)

        return _ok(result)

    @http.route("/agent_api/upload_file", type="http", auth="public",
                methods=["POST"], csrf=False)
    def upload_file(self, **_kwargs):
        # Multipart, not JSON (document_generator.py posts `files=`/`data=`),
        # so plain `request.params` already has `session_id`/`api_key` and
        # `request.httprequest.files` has the upload — no _params() needed.
        params = dict(request.params)
        agent_settings, failure = _authenticated(params)
        if failure:
            return failure

        session_id = params.get("session_id")
        uploaded = request.httprequest.files.get("file")
        if not uploaded:
            return _error(400, "No file uploaded.")

        env = _env_scoped_to(agent_settings)
        try:
            result = svc.save_generated_file(
                env, session_id, uploaded.filename, uploaded.read(), agent_settings,
            )
        except svc.MissingParameterError as exc:
            return _error(400, f"Missing {exc.parameter_name} parameter.")
        except svc.ResourceNotFoundError as exc:
            return _error(404, str(exc))
        except svc.FileTooLargeError as exc:
            return _error(413, f"The generated report exceeds the "
                               f"{exc.limit_bytes // (1024 * 1024)} MB limit.")

        return _ok(result)

    @http.route("/agent_api/messaging_config", type="http", auth="public",
                methods=["POST"], csrf=False)
    def messaging_config(self, **_kwargs):
        params = _params()
        agent_settings, failure = _authenticated(params)
        if failure:
            return failure
        return _ok(svc.get_messaging_config(_env_scoped_to(agent_settings)))

    @http.route("/agent_api/send_message", type="http", auth="public",
                methods=["POST"], csrf=False)
    def send_message(self, **_kwargs):
        params = _params()
        agent_settings, failure = _authenticated(params)
        if failure:
            return failure
        try:
            result = svc.send_message(_env_scoped_to(agent_settings), **params)
        except svc.ChannelNotConfiguredError as exc:
            return _error(409, exc.detail)
        return _ok(result)
