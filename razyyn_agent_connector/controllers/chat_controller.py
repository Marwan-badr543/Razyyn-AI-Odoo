# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""The chat page itself: Odoo's counterpart to the Frappe app's
``accountant_agent/page/agent_chat``.

WHY THIS IS A PLAIN SERVER-RENDERED PAGE, NOT AN OWL COMPONENT
    Odoo's web client asset bundles and OWL's own APIs have moved between
    17.0 and 18.0 (this connector ships identically for both — see the repo
    README's "Known gaps" for the one file that does diverge). A page built
    from a `<script>` tag in a QWeb template and vanilla `fetch()` has no
    OWL/bundle version to drift with; it is also exactly the shape
    ``razyyn-frappe-15``'s own chat page keeps client-side (jQuery-free
    hand-written JS against a fixed API contract), so the two stay
    comparable rather than one being framework-native and the other not.

WHY SIGN-UP MINTS THE RAZYYN.AGENT.SETTINGS KEY HERE, SERVER-SIDE
    Mirrors ``accountant_agent/page/agent_chat/agent_chat.py``'s
    ``authenticate_agent``: the customer presses Connect and never sees, and
    is never asked to paste, an API key. Generating it here means the same
    ``razyyn.agent.settings.generate()`` call the "Settings > Technical"
    manual-connection path uses, so both paths mint the exact same key
    shape — no second implementation to keep in sync.

WHY REQUESTS ARE type="http" WITH A HAND-ROLLED JSON READER, NOT type="json"
    Same reasoning as ``controllers/main.py``'s ``_params()`` — see that
    module's docstring. This controller's own callers are this page's own
    `fetch()` calls, so the choice is really about staying consistent with
    the rest of this addon rather than a hard requirement here.
"""

from __future__ import annotations

import json
import logging

import requests
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

#: Where the razyyn agent server lives. Overridable per-deployment via
#: Settings > Technical > Parameters (`razyyn_agent_connector.server_url`) —
#: the same idea as the Frappe app's `accountant_agent_server_url` site
#: config key, just Odoo's own config-parameter mechanism instead.
_SERVER_URL_PARAM = "razyyn_agent_connector.server_url"
_DEFAULT_SERVER_URL = "http://localhost:8010"

#: Registration/login/refresh are one quick round trip; ``/agent/ask`` runs
#: a full agent turn (tool calls, an LLM round trip per tool) and can
#: legitimately take much longer — matching the razyyn backend's own
#: DEFAULT_ERP_QUERY_TIMEOUT_SECONDS being separate from a plain HTTP call's
#: budget.
_AUTH_TIMEOUT_SECONDS = 15
_ASK_TIMEOUT_SECONDS = 120


def _server_url() -> str:
    return request.env["ir.config_parameter"].sudo().get_param(
        _SERVER_URL_PARAM, _DEFAULT_SERVER_URL,
    ).rstrip("/")


def _site_url() -> str:
    """This Odoo instance's own address, the way ``company_url`` is meant to
    read it — the same value ``razyyn_agent_connector``'s ``execute_query``
    etc. are reached on from the outside."""
    return request.httprequest.url_root.rstrip("/")


def _params() -> dict:
    if (request.httprequest.mimetype or "").endswith("json"):
        try:
            body = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
    return dict(request.params)


def _error(status_code: int, message: str):
    return request.make_json_response({"error": message}, status=status_code)


def _ok(payload):
    return request.make_json_response({"message": payload})


def _server_error_detail(response: requests.Response) -> str:
    """The razyyn server's own words, the same unwrapping
    ``connect.py``'s ``_platform_error`` does on the Frappe side."""
    try:
        body = response.json()
    except ValueError:
        return "The Razyyn service could not complete that request."
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list) and detail:
        first = detail[0]
        detail = first.get("msg") if isinstance(first, dict) else str(first)
    if isinstance(detail, str) and detail.strip():
        return detail
    return "The Razyyn service could not complete that request."


def _login_record():
    """This Odoo user's OWN chat login, or an empty recordset.

    Runs as the calling user (not sudo), so ``razyyn_chat_login_own_only``
    (security/chat_login_rules.xml) is what actually enforces "own", the
    same way ``get_agent_settings_doc``'s owner check does on the Frappe
    side — this call is the read, the ir.rule is the guarantee.
    """
    return request.env["razyyn.chat.login"].search(
        [("user_id", "=", request.env.uid)], limit=1,
    )


def _server_login(email: str, password: str) -> tuple[str, str] | None:
    """``(access_token, refresh_token)``, or ``None`` on any failure."""
    try:
        response = requests.post(
            f"{_server_url()}/auth/login",
            json={"username": email, "password": password},
            timeout=_AUTH_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        _logger.warning("Razyyn login request error: %s", exc)
        return None
    if response.status_code != 200:
        return None
    body = response.json()
    return body.get("access_token"), body.get("refresh_token")


def _server_refresh(refresh_token: str) -> tuple[str, str] | None:
    """A fresh ``(access_token, refresh_token)`` pair, or ``None``.

    The razyyn server rotates refresh tokens — the old one stops working the
    moment this succeeds — so both halves of the pair are always saved
    together, never just the access token alone.
    """
    try:
        response = requests.post(
            f"{_server_url()}/auth/refresh",
            json={"refresh_token": refresh_token},
            timeout=_AUTH_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        _logger.warning("Razyyn refresh request error: %s", exc)
        return None
    if response.status_code != 200:
        return None
    body = response.json()
    return body.get("access_token"), body.get("refresh_token")


class RazyynChatController(http.Controller):

    @http.route("/razyyn/chat", type="http", auth="user")
    def chat_page(self, **_kwargs):
        return request.render("razyyn_agent_connector.chat_page", {})

    @http.route("/razyyn/status", type="http", auth="user",
                methods=["GET"], csrf=False)
    def status(self, **_kwargs):
        login = _login_record()
        return _ok({"connected": bool(login), "email": login.email if login else None})

    @http.route("/razyyn/authenticate", type="http", auth="user",
                methods=["POST"], csrf=False)
    def authenticate(self, **_kwargs):
        params = _params()
        mode = params.get("mode")
        email = (params.get("email") or "").strip()
        password = params.get("password") or ""
        company_name = (params.get("company_name") or "").strip()

        if not email or not password:
            return _error(400, "Email and password are required.")

        existing = _login_record()

        if mode == "signup":
            if existing:
                return _error(
                    409, "This Odoo account is already connected to a Razyyn "
                         "login. Use Login instead, or disconnect first.",
                )
            if not company_name:
                return _error(400, "Company name is required to sign up.")

            settings_record, plaintext_key = request.env["razyyn.agent.settings"].sudo().generate(
                label=f"Chat: {email}",
            )
            # Odoo does not commit a request's transaction until the request
            # returns. The razyyn server call below makes the razyyn SERVER
            # call straight back into THIS Odoo instance's own
            # /agent_api/execute_query to verify the key — a separate HTTP
            # request, on a separate DB connection, that cannot see this row
            # until it is committed. Same reason
            # accountant_agent/page/agent_chat/agent_chat.py's
            # save_agent_settings() calls frappe.db.commit() right after
            # writing Agent Settings, before the equivalent outbound call.
            request.env.cr.commit()

            try:
                response = requests.post(
                    f"{_server_url()}/users/",
                    json={
                        "name": company_name,
                        "username": email,
                        "password": password,
                        "company_url": _site_url(),
                        "api_key": plaintext_key,
                        "erp_code": "ODOO",
                    },
                    timeout=_AUTH_TIMEOUT_SECONDS,
                )
            except requests.exceptions.RequestException as exc:
                settings_record.sudo().unlink()
                _logger.warning("Razyyn registration request error: %s", exc)
                return _error(503, "Could not reach the Razyyn service. "
                                    "Please make sure it is running.")

            if response.status_code != 201:
                settings_record.sudo().unlink()
                return _error(response.status_code, _server_error_detail(response))

            tokens = _server_login(email, password)
            if tokens is None:
                # The account exists on the server now; only the immediate
                # sign-in failed (a slow server, a dropped connection). Do
                # NOT delete the settings record here — undoing it would
                # orphan an account the server already created, and the
                # customer's next Login attempt is the correct recovery
                # path, not a silent retry of Sign Up.
                return _error(502, "Account created, but signing in failed. "
                                    "Please try Login.")
            access_token, refresh_token = tokens

            request.env["razyyn.chat.login"].sudo().create({
                "user_id": request.env.uid,
                "email": email,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "agent_settings_id": settings_record.id,
            })
            return _ok({"email": email})

        elif mode == "login":
            if not existing:
                return _error(
                    404, "No Razyyn account connected for this Odoo user yet. "
                         "Please sign up first.",
                )

            tokens = _server_login(email, password)
            if tokens is None:
                return _error(401, "Could not sign in. Check the email and password.")
            access_token, refresh_token = tokens

            existing.sudo().write({
                "email": email, "access_token": access_token, "refresh_token": refresh_token,
            })
            return _ok({"email": email})

        return _error(400, "Invalid mode.")

    @http.route("/razyyn/disconnect", type="http", auth="user",
                methods=["POST"], csrf=False)
    def disconnect(self, **_kwargs):
        """Forget this Odoo user's sign-in. The razyyn.agent.settings key
        this account minted is left active — other signed-in users' chat
        sessions (if any) and any Creator-agent write connection keep
        working, matching how disconnect_agent behaves on the Frappe side:
        it clears the connection, not the credential.
        """
        existing = _login_record()
        if existing:
            existing.sudo().unlink()
        return _ok({"connected": False})

    @http.route("/razyyn/ask", type="http", auth="user",
                methods=["POST"], csrf=False)
    def ask(self, **_kwargs):
        params = _params()
        login = _login_record()
        if not login:
            return _error(401, "Not connected to Razyyn yet. Please sign in or sign up.")

        message = params.get("message")
        if not message:
            return _error(400, "Message is required.")

        form = {
            "message": message,
            "erp_system": "ODOO",
            "history": params.get("history") or "",
            "custom_instructions": params.get("custom_instructions") or "",
            "session_id": params.get("session_id") or "",
            "stream": "false",
        }

        def _send(token: str) -> requests.Response:
            return requests.post(
                f"{_server_url()}/agent/ask", data=form,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_ASK_TIMEOUT_SECONDS,
            )

        try:
            response = _send(login.access_token)
            if response.status_code == 401 and login.refresh_token:
                # Same "refresh once on 401" shape as connect.py's
                # _platform_request — a customer typing a message should
                # never have to notice their access token quietly expired.
                refreshed = _server_refresh(login.refresh_token)
                if refreshed is None:
                    login.sudo().unlink()
                    return _error(401, "Your Razyyn session has expired. "
                                        "Please sign in again.")
                new_access, new_refresh = refreshed
                login.sudo().write({"access_token": new_access, "refresh_token": new_refresh})
                response = _send(new_access)
        except requests.exceptions.RequestException as exc:
            _logger.warning("Razyyn ask request error: %s", exc)
            return _error(503, "Could not reach the Razyyn service. Please try again.")

        if response.status_code != 200:
            return _error(response.status_code, _server_error_detail(response))

        return _ok(response.json())
