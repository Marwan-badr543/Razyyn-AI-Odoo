# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Controller layer — the Razyyn write gateway, on Odoo.

Seven routes, one per protocol method, at ``/agent_write/<method>``. The names
are the protocol's own (``agent/agent_create/adapters/gateway.py``,
``PROTOCOL_METHODS``) rather than names of this module's choosing, because the
agent builds its URL from the method name directly — a route called something
else is a route the agent will never reach, and the symptom is a 404 the
Creator Agent reports as "your system refused this request".

HTTP parsing and exception-to-status mapping only. No ORM calls and no business
validation here — that is ``services/agent_write_service.py`` — matching the
three-layer split the Frappe app and the agent's own backend both follow.
"""

from __future__ import annotations

import json
import logging

from odoo import http
from odoo.http import request

from ..services import agent_api_service as svc
from ..services import agent_write_service as write_svc
from ..services.write_guard import AgentWriteError

logger = logging.getLogger(__name__)


def _params() -> dict:
    """The request's own parameters, from a JSON body or from form/query.

    Odoo's ``type="http"`` dispatcher builds a route's ``**kwargs`` from the
    query string and form-encoded bodies, never from a raw ``application/json``
    one. The agent posts JSON, so a route reading only ``kwargs`` receives an
    empty dict — and the first symptom is "missing API key", because the key is
    in that same unread body.
    """
    if (request.httprequest.mimetype or "").endswith("json"):
        try:
            body = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
    return dict(request.params)


def _api_key(params: dict) -> str:
    """The caller's key, from wherever it travelled.

    ``Authorization: token <key>`` is split on ':' because the protocol's other
    implementation authenticates with a ``key:secret`` pair and the agent sends
    the same header shape to both. Odoo has only the key, which is the part
    before the colon.
    """
    header = request.httprequest.headers.get("Authorization", "")
    from_header = header.replace("token ", "").replace("Bearer ", "").split(":")[0]
    return (
        params.get("api_key")
        or request.httprequest.headers.get("X-API-Key")
        or from_header
    )


def _error(status_code: int, message: str, code: str = "WRITE_REJECTED"):
    # `error` and `error_code` both, because the agent turns the code into the
    # sentence it shows a customer and falls back to the message only when
    # there is no code. A refusal with neither reaches them as "your system
    # refused this request", which tells them nothing they can act on.
    return request.make_json_response({"error": message, "error_code": code},
                                      status=status_code)


def _ok(payload):
    return request.make_json_response({"message": payload})


def _authenticated(params):
    key = _api_key(params)
    if not key:
        return None, _error(401, "Missing API Key. Authentication required.", "AUTH_REQUIRED")
    settings = svc.authenticate_by_api_key(request.env, key)
    if not settings:
        return None, _error(403, "Invalid API Key. Authentication failed.", "AUTH_FAILED")
    return settings, None


def _service_env():
    """An environment run as the platform's own service account (uid 1).

    The agent is a machine caller authenticated by API key, not a logged-in
    person, and ``razyyn.agent.settings`` carries no Odoo user of its own to
    run as. What the agent may WRITE is decided by the Agent Write Policy
    record, read fresh inside the service layer on every call — not by this
    environment's privileges.
    """
    return request.env(user=1)


def _json_param(value, default):
    """A parameter that may arrive as JSON text or as a real object.

    The agent sends ``documents`` as a JSON STRING (Frappe's form encoding
    cannot carry a nested list, so the protocol settled on text) but a JSON
    body delivers it already parsed. Accepting only one shape silently loses
    every document in the batch.
    """
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value




def _the_connections_company(settings):
    """The company this API key was issued for, as an id, or None.

    WHICH SET OF BOOKS THE AGENT IS WRITING IN. A business with two companies
    has two journals called "Miscellaneous Operations", two accounts called
    "Bank" and two of nearly everything else — correctly, because they are two
    sets of books. Asked to record an entry in "Miscellaneous Operations", the
    write side found both and refused the document as ambiguous, naming the
    same journal to the customer twice.

    The connection already answers this. It is issued against one company, the
    field is required on it, and the READ side has passed it down since the
    per-company columns arrived. This is the write side catching up: the same
    fact, from the same record, so a name means the same thing whichever half
    of the connector is asked.
    """
    company = getattr(settings, "company_id", None)
    return company.id if company else None


class RazyynAgentWriteController(http.Controller):
    """The seven protocol methods."""

    # ── Reading the contract ─────────────────────────────────────────────────

    @http.route("/agent_write/get_document_spec", type="http", auth="public",
                methods=["POST"], csrf=False)
    def get_document_spec(self, **_kwargs):
        params = _params()
        _settings, failure = _authenticated(params)
        if failure:
            return failure
        try:
            spec = write_svc.build_document_spec(
                _service_env(), params.get("doctype") or params.get("model")
            )
        except AgentWriteError as exc:
            return _error(400, exc.message, exc.code)
        return _ok({"spec": spec})

    @http.route("/agent_write/search_documents", type="http", auth="public",
                methods=["POST"], csrf=False)
    def search_documents(self, **_kwargs):
        params = _params()
        _settings, failure = _authenticated(params)
        if failure:
            return failure
        result = write_svc.search_documents(
            _service_env(),
            doctypes=_json_param(params.get("doctypes"), []),
            text=params.get("text") or "",
            docstatus=_json_param(params.get("docstatus"), []),
            limit=int(params.get("limit") or 10),
            company=params.get("company") or "",
        )
        return _ok(result)

    @http.route("/agent_write/get_write_policy", type="http", auth="public",
                methods=["POST", "GET"], csrf=False)
    def get_write_policy(self, **_kwargs):
        params = _params()
        _settings, failure = _authenticated(params)
        if failure:
            return failure
        return _ok({"policy": write_svc.load_write_policy(_service_env())})

    @http.route("/agent_write/get_write_log", type="http", auth="public",
                methods=["POST"], csrf=False)
    def get_write_log(self, **_kwargs):
        params = _params()
        _settings, failure = _authenticated(params)
        if failure:
            return failure
        return _ok({"log": write_svc.get_write_log(
            _service_env(), params.get("idempotency_key") or ""
        )})

    @http.route("/agent_write/get_document", type="http", auth="public",
                methods=["POST"], csrf=False)
    def get_document(self, **_kwargs):
        """One record by its own reference. See `read_document_state`."""
        params = _params()
        _settings, failure = _authenticated(params)
        if failure:
            return failure
        return _ok({"document": write_svc.read_document_state(
            _service_env(),
            params.get("doctype") or params.get("model") or "",
            params.get("docname") or "",
        )})

    @http.route("/agent_write/list_documents", type="http", auth="public",
                methods=["POST"], csrf=False)
    def list_documents(self, **_kwargs):
        params = _params()
        _settings, failure = _authenticated(params)
        if failure:
            return failure
        return _ok({"documents": write_svc.list_agent_documents(
            _service_env(),
            limit=int(params.get("limit") or 20),
            doctype=params.get("doctype") or params.get("model") or "",
        )})

    # ── Checking ─────────────────────────────────────────────────────────────

    @http.route("/agent_write/preflight", type="http", auth="public",
                methods=["POST"], csrf=False)
    def preflight(self, **_kwargs):
        params = _params()
        settings, failure = _authenticated(params)
        if failure:
            return failure
        payload = _json_param(params.get("payload"), {}) or {}
        if not isinstance(payload, dict):
            return _error(400, "The payload must be an object.", "BAD_PAYLOAD")
        try:
            report = write_svc.preflight_document(
                _service_env(), payload,
                run_dry_run=bool(int(params.get("run_dry_run") or 0)),
                default_company=_the_connections_company(settings),
            )
        except AgentWriteError as exc:
            return _error(400, exc.message, exc.code)
        # `preflight`, which is the key the client reads. `report` — what this
        # answered before — left the agent with no findings at all, so every
        # payload passed validation and was written unchecked.
        return _ok({"preflight": report})

    # ── Writing ──────────────────────────────────────────────────────────────

    @http.route("/agent_write/write_batch", type="http", auth="public",
                methods=["POST"], csrf=False)
    def write_batch(self, **_kwargs):
        params = _params()
        settings, failure = _authenticated(params)
        if failure:
            return failure

        documents = _json_param(params.get("documents"), [])
        if not isinstance(documents, list):
            return _error(400, "documents must be a list.", "BAD_PAYLOAD")

        try:
            batch = write_svc.write_documents_batch(
                _service_env(), documents,
                run_id=params.get("run_id") or "",
                session_id=params.get("session_id") or "",
                approved_by=params.get("approved_by") or "",
                default_company=_the_connections_company(settings),
            )
        except AgentWriteError as exc:
            # The WHOLE request was refused and nothing was written. The agent
            # turns this into one refusal per document, so the customer's
            # report names every entry that did not make it rather than a count
            # that does not add up.
            return _error(422, exc.message, exc.code)
        except Exception:
            # Never the raw exception: it carries table names and file paths.
            # The detail goes to Odoo's log, where an administrator can see it
            # and a caller cannot.
            logger.exception("Agent write batch failed")
            return _error(500, "The request could not be completed.", "INTERNAL_ERROR")

        return _ok({"batch": batch})
