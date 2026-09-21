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
import logging

from odoo import http
from odoo.http import request

from ..services import agent_api_service as svc
from ..services import agent_messaging_service as messaging
from ..services import live_rows
from ..services.query_guard import ForbiddenQueryError

logger = logging.getLogger(__name__)


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


def _service_env():
    """An environment run as the platform's own service account (uid 1).

    Uid 1 rather than the calling accountant's own user: the agent is a machine
    caller authenticated by API key, not a logged-in person, and
    ``razyyn.agent.settings`` carries no Odoo user of its own to run as — the
    same "reading needs no grant" position the Frappe app documents in
    ``agent_api_repository.py``.

    NO ``allowed_company_ids`` CONTEXT, DELIBERATELY
        An earlier revision passed one here and described it as bounding every
        read to the connection's company. It never did. Uid 1 is Odoo's
        ``SUPERUSER_ID``, and Odoo's ``Environment`` factory forces
        ``env.su = True`` for it, so ir.rule record rules — the mechanism
        ``allowed_company_ids`` feeds — do not run at all. Carrying a context
        that enforces nothing while reading as though it does is worse than
        carrying none: the next person to touch this file trusts it.

        Where the company boundary actually sits is written up in
        ``agent_api_service.validate_and_execute_query``: as on the Frappe app,
        it is read-only plus the guard's denylist, and the company is a default
        the agent scopes its own questions by, not a filter this layer imposes.
    """
    return request.env(user=1)


def _error(status_code, message, code=""):
    """A refusal the caller can act on.

    ``error_code`` travels beside the sentence because the agent turns a code
    into the words it shows a customer and falls back to the sentence only
    when there is none. It is omitted rather than sent empty so that older
    readers see exactly what they saw before.
    """
    body = {"error": message}
    if code:
        body["error_code"] = code
    return request.make_json_response(body, status=status_code)


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
        try:
            result = svc.validate_and_execute_query(
                _service_env(), sql_query,
                # Draft and cancelled rows are removed from every table the
                # statement reads unless the caller asks for them by name —
                # see services/live_rows.py.
                include_cancelled=live_rows.read_flag(params.get("include_cancelled")),
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
        env = _service_env()
        try:
            result = svc.build_schema_summary(env, model_name, agent_settings)
        except svc.MissingParameterError:
            return _error(400, "Missing model parameter.")
        except svc.ResourceNotFoundError as exc:
            return _error(404, str(exc))

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

        env = _service_env()
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
        return _ok(messaging.get_messaging_config(_service_env()))

    @http.route("/agent_api/send_message", type="http", auth="public",
                methods=["POST"], csrf=False)
    def send_message(self, **_kwargs):
        """Send one message, and answer with a receipt or with a reason.

        A refusal MUST come back as a non-200 carrying ``error``: the agent
        treats a 200 without a provider message id as "could not be sent" but
        has nothing to tell the customer, so an accountant is left with
        "that could not be sent: the system replied with HTTP 200".
        """
        params = _params()
        agent_settings, failure = _authenticated(params)
        if failure:
            return failure
        try:
            result = messaging.send_message(_service_env(), **params)
        except messaging.ChannelNotConfiguredError as exc:
            return _error(409, exc.detail, exc.code)
        except messaging.MessagingError as exc:
            # Refused here (an unlisted destination, an attachment this site
            # does not hold) or refused by the provider. Either way nothing was
            # delivered and the reason is the customer's to read.
            return _error(400, exc.detail, exc.code)
        except Exception:
            # Never the raw exception: it carries table names, file paths, and
            # on a provider error sometimes the request body itself.
            logger.exception("Sending a message failed")
            return _error(500, "The message could not be sent.", "INTERNAL_ERROR")
        return _ok(result)
