# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""What the chat window asks its server for.

ONE ROUTE PER FRAPPE METHOD, AND THE SAME NAME
    The chat window is the Frappe app's, copied file for file (see
    tools/sync_from_frappe.py). It addresses its server by Python path --
    `...agent_chat.agent_chat.get_chats` -- and razyyn_platform.js turns that
    into `/razyyn/api/get_chats`. So every route below is named after the
    Frappe method it answers, and returns the same shape.

    That is a constraint worth stating: a route here may not invent a nicer
    reply. If `get_turn_result` answers `{"status": "pending"}` on ERPNext, it
    answers `{"status": "pending"}` here, because the code reading it is the
    same code. Anything genuinely Odoo-shaped belongs behind the route, not in
    its answer.

WHY type="json"
    A browser cannot send `Content-Type: application/json` to another origin
    without a CORS preflight, and Odoo grants none. So these routes cannot be
    reached by a form on somebody else's site, which is what the CSRF token
    exists to prevent for ordinary form posts. The one route that does take a
    form -- the file upload, because a file is multipart -- keeps the token.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid

import requests
import werkzeug

from odoo import fields, http
from odoo.exceptions import AccessDenied, UserError
from odoo.http import request

from ..services import chat_turn_service as turns
from ..services import session_holder
from ..services import ocr_service

_logger = logging.getLogger(__name__)

# ── Upload rules. The browser is told these rather than keeping its own copy ──
ALLOWED_EXTENSIONS = (
    ".pdf", ".docx", ".odt", ".xlsx", ".ods", ".pptx", ".odp",
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")
EXCEL_EXTENSIONS = (".xlsx", ".xls", ".ods")
MAX_UPLOAD_FILES = 5
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024

REGISTER_TIMEOUT = (10, 30)
LOGIN_TIMEOUT = (10, 30)
DELETE_ACCOUNT_TIMEOUT = (10, 30)
BANNER_TIMEOUT = (5, 10)


def _ok(payload):
    """Every reply the chat window reads is `{"message": ...}`, as on Frappe."""
    return {"message": payload}


def _server_url() -> str:
    return turns.server_url(request.env)


def _settings(email: str | None = None):
    """This user's connection record.

    Scoped to the signed-in user, always. The e-mail is what the browser
    believes it is connected as; it narrows the search but can never widen it,
    which is what stops one signed-in user from naming another's account.
    """
    domain = [("user_id", "=", request.env.uid)]
    if email:
        domain.append(("email", "=", email))
    return request.env["razyyn.agent.settings"].sudo().search(domain, limit=1)


def _discard(settings) -> None:
    """Remove a half-made connection. Cannot fail the request that is failing.

    It used to be a bare `settings.unlink()` inside an `except`, and when the
    delete lost a race -- "could not serialize access due to concurrent
    update", seen here for real -- the database error REPLACED the reason the
    customer needed to read. They were told about a serialisation failure
    instead of "This ERP is already linked to an account".

    A row left behind is not the end of anything now: the next attempt reuses
    an unused one rather than refusing because of it.

    WHY THE DELETE LOSES, AND IT IS NOT RARE
        Registering hands the platform a key and the platform immediately calls
        this Odoo back to try it. That callback is a separate request in a
        separate transaction, and it WRITES to the same row -- `last_used`. So
        the delete is racing the verification of the very key it is deleting,
        and on a refused registration it loses about every time.

        What must not survive is the KEY, because a key nobody is watching
        still opens the write API. So if the row cannot go, the key is retired
        where it stands.
    """
    try:
        settings.sudo().unlink()
        request.env.cr.commit()
        return
    except Exception as exc:
        request.env.cr.rollback()
        _logger.warning(
            "Razyyn AI: could not remove the half-made connection %s: %s",
            settings.id, exc,
        )

    try:
        settings.sudo().reissue_api_key()
        request.env.cr.commit()
        _logger.info(
            "Razyyn AI: retired the key on connection %s, which could not be "
            "removed. It holds no session and the next sign-up will reuse it.",
            settings.id,
        )
    except Exception as exc:
        request.env.cr.rollback()
        _logger.warning(
            "Razyyn AI: could not retire the key on connection %s: %s",
            settings.id, exc,
        )


def _mine(email: str | None = None):
    """This user's Razyyn connection. The e-mail narrows it; it cannot hide it.

    Every route below is asking the same question -- "which connection belongs
    to the person making this request" -- and every one of them was answering
    it with `_settings(agent_email)`, where the e-mail is simply what the
    BROWSER last wrote to localStorage. So a remembered address that no longer
    matched the record silently meant NO CONNECTION, and the route acted as if
    the customer had never signed in: the page showed a sign-in card, Send did
    nothing, Disconnect reported failure. One record on this developer's own
    database had an empty e-mail column (the old connector kept it in the
    label) and that was enough to lock its owner out of all of it.

    Narrowing is still worth doing -- it picks the right one when somebody has
    two -- but the search is scoped to `request.env.uid` either way, so falling
    back cannot reach anybody else's connection.
    """
    return _settings(email) or _settings()


def _session_holder(email: str):
    """Where this user's Razyyn session is kept. See services/session_holder.py.

    A thin pass-through on purpose: the rule below decided who can sign in and
    who cannot, so it belongs somewhere a test can call it with an `env` rather
    than only through a live HTTP request.
    """
    return session_holder.settings_for_session(request.env, request.env.uid, email)


def _session(session_id: str):
    """The caller's own conversation, or an empty recordset.

    Ownership is in the search, not in a check after it: a conversation that is
    not yours is not found, so there is no path where a later `if` decides
    whether to let you have it.
    """
    if not session_id:
        return request.env["razyyn.agent.chat.session"].sudo().browse(())
    return request.env["razyyn.agent.chat.session"].sudo().search(
        [("session_id", "=", session_id), ("user_id", "=", request.env.uid)], limit=1
    )


def _owned_session_or_refuse(session_id: str):
    session = _session(session_id)
    if not session:
        raise UserError(request.env._("Chat session not found."))
    return session


def _as_bool(value) -> bool:
    """Whether a switch sent by the browser is on.

    A form field arrives as text, and "false" is a perfectly true string. Every
    place that reads one of these agrees on what "on" means because they all
    come through here.
    """
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _row(session) -> dict:
    """One conversation, in the shape the chat window's sidebar reads."""
    return {
        # Frappe names a chat record by its session id, and the browser uses
        # `name` as the key. Same string here, so the copied code needs no edit.
        "name": session.session_id,
        "session_id": session.session_id,
        "title": session.title,
        "last_update": fields.Datetime.to_string(session.last_update),
        "creation": fields.Datetime.to_string(session.create_date),
    }


def _message_row(message) -> dict:
    stamp = fields.Datetime.to_string(message.create_date)
    return {
        "name": str(message.id),
        "sender": message.sender,
        "content": message.content,
        "creation": stamp,
        "creation1": fields.Datetime.to_string(message.created_at) or stamp,
    }


def _jwt_is_expired(token: str) -> bool:
    """Read a JWT expiry for UI state only; the platform still verifies it.

    This is not an authorization decision. It prevents a non-empty but expired
    access token from making the page say Connected forever after renewal has
    become impossible.
    """
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return float(payload.get("exp") or 0) <= time.time()
    except (ValueError, TypeError, IndexError, KeyError, json.JSONDecodeError):
        return True


class RazyynChatApi(http.Controller):
    """The chat window's server. One route per Frappe whitelisted method."""

    # ── Connection ──────────────────────────────────────────────────────────

    @http.route("/razyyn/api/get_connection_status", type="json", auth="user")
    def get_connection_status(self, agent_email=None, **_kwargs):
        # The e-mail is what the BROWSER last remembered, and it can be out of
        # date -- a different account signed in since, or a record whose column
        # an older version never filled in. It narrows the search; it must not
        # be able to hide a session this user really has, or the page shows a
        # sign-in card to somebody who is signed in. The answer carries the
        # address actually on file, so the browser corrects itself.
        settings = _mine(agent_email)
        if settings and settings.access_token:
            if _jwt_is_expired(settings.sudo().access_token):
                if not turns.renew_access_token(request.env, settings):
                    settings.sudo().write({
                        "access_token": False,
                        "refresh_token": False,
                    })
                    return _ok({"connected": False, "email": None})
            return _ok({"connected": True, "email": settings.email})
        return _ok({"connected": False, "email": None})

    @http.route("/razyyn/api/authenticate_agent", type="json", auth="user")
    def authenticate_agent(self, mode=None, email=None, password=None,
                           company_name=None, **_kwargs):
        email = (email or "").strip()
        if not email or not password:
            raise UserError(request.env._("Email and password are required."))

        if mode == "signup":
            return _ok(self._sign_up(email, password, company_name))
        if mode == "login":
            return _ok(self._sign_in(email, password))
        raise UserError(request.env._("Invalid mode specified."))

    def _sign_up(self, email, password, company_name):
        company_name = (company_name or request.env.company.name or "").strip()
        if not company_name:
            raise UserError(request.env._("Company Name is required for registration."))
        # `_settings`, not `_mine`: this asks whether a record for THIS address
        # already exists, and the fallback would answer yes about a different
        # one and refuse a legitimate sign-up.
        #
        # A record IN USE means signing up again is the wrong door -- that is
        # what the refusal is for. A record with no session and no registered
        # connection is the leftovers of an attempt that did not finish, and
        # refusing on account of it makes a permanent dead end out of one
        # failed registration. It is reused instead, with a fresh key.
        existing = _settings(email)
        if existing and (existing.sudo().access_token or existing.erp_connection_id):
            raise UserError(request.env._("A connection already exists for %(email)s.", email=email))

        # The key is minted BEFORE the account, because registration hands it to
        # the platform: it is how the agent gets back in to read this Odoo.
        if existing:
            settings = existing
            api_key = settings.reissue_api_key()
            settings.write({"label": f"Razyyn AI: {email}", "email": email})
        else:
            settings, api_key = request.env["razyyn.agent.settings"].sudo().generate(
                user_id=request.env.uid, label=f"Razyyn AI: {email}", email=email,
            )
        request.env.cr.commit()

        base_url = request.env["ir.config_parameter"].sudo().get_param(
            "web.base.url", "http://localhost:8069"
        ).rstrip("/")
        try:
            response = requests.post(
                f"{_server_url()}/users/",
                json={
                    "name": company_name,
                    "username": email,
                    "password": password,
                    "company_url": base_url,
                    "api_key": api_key,
                    "erp_code": "ODOO",
                },
                timeout=REGISTER_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            # The half-made record goes, or the next attempt is refused as a
            # duplicate of an account that was never created.
            _discard(settings)
            _logger.warning("Razyyn AI: registration could not be sent: %s", exc)
            raise UserError(
                request.env._("Could not reach the Razyyn service. Please make sure it is running.")
            ) from exc

        if response.status_code != 201:
            _discard(settings)
            # The commonest refusal here is "This ERP is already linked to an
            # account", and the person reading it has no way to know that the
            # Login tab is now the answer -- it used to refuse them too.
            raise UserError(
                self._detail(response, "Registration failed.") + "\n\n"
                "If you already have a Razyyn account, use Login instead."
            )

        tokens = self._login_on_platform(email, password)
        settings.write(tokens)
        return {"success": True, "email": email, "api_key": api_key}

    def _sign_in(self, email, password):
        tokens = self._login_on_platform(email, password)
        # The e-mail goes on with the tokens. A record whose column was left
        # empty by an older version is corrected here, the first time its owner
        # signs in, without anybody having to know it was wrong.
        _session_holder(email).write(dict(tokens, email=email))
        return {"success": True, "email": email, "api_key": ""}

    def _login_on_platform(self, email, password) -> dict:
        try:
            response = requests.post(
                f"{_server_url()}/auth/login",
                json={"username": email, "password": password},
                timeout=LOGIN_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            _logger.warning("Razyyn AI: sign-in could not be sent: %s", exc)
            raise UserError(
                request.env._("Could not reach the Razyyn service. Please make sure it is running.")
            ) from exc
        if response.status_code != 200:
            raise UserError(self._detail(
                response, "Sign-in failed. Please check your email and password."
            ))
        try:
            body = response.json()
        except (ValueError, TypeError) as exc:
            raise UserError(
                request.env._("The Razyyn service returned an invalid sign-in response. Please try again.")
            ) from exc

        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise UserError(
                request.env._("The Razyyn service did not return an access token. Please try again.")
            )
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            raise UserError(
                request.env._("The Razyyn service did not return a refresh token. Please try again.")
            )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
        }

    @staticmethod
    def _detail(response, fallback: str) -> str:
        try:
            return str(response.json().get("detail") or fallback)
        except Exception:
            return fallback

    @http.route("/razyyn/api/disconnect_agent", type="json", auth="user")
    def disconnect_agent(self, agent_email=None, **_kwargs):
        settings = _mine(agent_email)
        if not settings:
            return _ok({"success": False})
        settings.write({"access_token": False, "refresh_token": False})
        return _ok({"success": True})

    @http.route("/razyyn/api/delete_agent_account", type="json", auth="user")
    def delete_agent_account(self, agent_email=None, **_kwargs):
        settings = _mine(agent_email)
        if not settings:
            return _ok({"success": False})

        identifier = self._platform_user_id(settings)
        if identifier:
            try:
                response = requests.delete(
                    f"{_server_url()}/users/{identifier}", timeout=DELETE_ACCOUNT_TIMEOUT
                )
            except requests.exceptions.RequestException as exc:
                _logger.warning("Razyyn AI: account deletion could not be sent: %s", exc)
                raise UserError(request.env._("Could not reach the Razyyn service. Please try again.")) from exc
            if response.status_code not in (200, 204, 404):
                raise UserError(request.env._("The agent account could not be deleted. Please try again."))

        settings.unlink()
        return _ok({"success": True})

    @staticmethod
    def _platform_user_id(settings) -> str:
        """Who the platform thinks we are, out of the access token's payload.

        The token is a JWT; its `sub` is the account id. Read without
        verifying, deliberately -- this is not a security decision, it is an
        address, and the platform checks the token itself on arrival.
        """
        import base64

        token = (settings.sudo().access_token or "").split(".")
        if len(token) < 2:
            return ""
        try:
            padded = token[1] + "=" * (-len(token[1]) % 4)
            return str(json.loads(base64.urlsafe_b64decode(padded)).get("sub") or "")
        except Exception:
            return ""

    @http.route("/razyyn/api/get_agent_settings_name", type="json", auth="user")
    def get_agent_settings_name(self, email=None, **_kwargs):
        """Which record to open when the customer clicks "Connected as ...".

        The chat window asks for a name and opens a form; on Odoo the name of a
        record is its id.
        """
        settings = _mine(email)
        return _ok(settings.id if settings else None)

    @http.route("/razyyn/api/set_user_language", type="json", auth="user")
    def set_user_language(self, language=None, **_kwargs):
        """Switch the signed-in user's language from the chat's own header.

        The picker offers two-letter codes; Odoo names a language by locale, and
        only the ones the site has actually installed can be chosen. A language
        that is not installed is refused with a sentence saying so -- silently
        doing nothing would look like the picker was broken.
        """
        wanted = (language or "").strip()[:2].lower()
        if not wanted:
            raise UserError(request.env._("No language was chosen."))

        installed = request.env["res.lang"].sudo().search([("active", "=", True)])
        match = next((lang for lang in installed if lang.code.lower().startswith(wanted)), None)
        if not match:
            raise UserError(request.env._(
                "That language is not installed in Odoo yet. An administrator can "
                "add it under Settings -> Translations -> Languages."
            ))
        request.env.user.sudo().write({"lang": match.code})
        return _ok({"success": True, "lang": match.code})

    # ── Conversations ───────────────────────────────────────────────────────

    @http.route("/razyyn/api/get_chats", type="json", auth="user")
    def get_chats(self, **_kwargs):
        sessions = request.env["razyyn.agent.chat.session"].sudo().search(
            [("user_id", "=", request.env.uid)], order="last_update desc, id desc"
        )
        return _ok([_row(session) for session in sessions])

    @http.route("/razyyn/api/create_chat_with_id", type="json", auth="user")
    def create_chat_with_id(self, session_id=None, title=None, **_kwargs):
        if not session_id:
            raise UserError(request.env._("Session ID is required."))
        # The client picks this identifier, so it must look like one the client
        # generated rather than an arbitrary string that could collide with, or
        # be mistaken for, another customer's session key.
        try:
            uuid.UUID(str(session_id))
        except (ValueError, AttributeError, TypeError):
            raise UserError(request.env._("Invalid session identifier."))
        if request.env["razyyn.agent.chat.session"].sudo().search_count(
            [("session_id", "=", session_id)]
        ):
            raise UserError(request.env._("Chat session already exists."))

        session = request.env["razyyn.agent.chat.session"].sudo().create({
            "session_id": session_id,
            "title": title or "New Chat",
            "user_id": request.env.uid,
            "last_update": fields.Datetime.now(),
        })
        return _ok(_row(session))

    @http.route("/razyyn/api/update_chat_title", type="json", auth="user")
    def update_chat_title(self, session_id=None, title=None, **_kwargs):
        if not title:
            raise UserError(request.env._("Session ID and Title are required."))
        session = _owned_session_or_refuse(session_id)
        session.write({"title": title, "last_update": fields.Datetime.now()})
        return _ok(_row(session))

    @http.route("/razyyn/api/delete_chat", type="json", auth="user")
    def delete_chat(self, session_id=None, **_kwargs):
        session = _session(session_id)
        if not session:
            return _ok({"success": False})
        # The events are a delivery buffer keyed by the conversation, not by a
        # foreign key, so nothing else would ever remove them.
        request.env["razyyn.agent.chat.event"].sudo().search(
            [("session_id", "=", session.session_id)]
        ).unlink()
        session.unlink()
        return _ok({"success": True})

    @http.route("/razyyn/api/get_chat_history", type="json", auth="user")
    def get_chat_history(self, session_id=None, limit=None, **_kwargs):
        """The caller's own transcript for one session, oldest first.

        Bounded: a year-old reconciliation thread is not something to serialise
        in full into a single HTTP response.
        """
        session = _session(session_id)
        if not session:
            return _ok([])
        try:
            page_size = min(int(limit or turns.MAX_HISTORY_PAGE_SIZE),
                            turns.MAX_HISTORY_PAGE_SIZE)
        except (TypeError, ValueError):
            page_size = turns.MAX_HISTORY_PAGE_SIZE

        recent = request.env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id)], order="id desc", limit=page_size
        )
        return _ok([_message_row(message) for message in reversed(recent)])

    # ── A turn ──────────────────────────────────────────────────────────────

    @http.route("/razyyn/api/send_message", type="json", auth="user")
    def send_message(self, message=None, session_id=None, agent_email=None,
                     agent_type="auto", file_urls=None, scan=False,
                     high_thinking=False, **_kwargs):
        """Accept the customer's message and hand the turn to a worker.

        Returns as soon as the work is accepted -- never when it is finished.
        The answer arrives over /razyyn/chat/events, and `previous_ai_message_name`
        is what lets the browser recognise a newer answer if it misses them.
        """
        message = message or ""
        session = _owned_session_or_refuse(session_id)

        settings = _mine(agent_email)
        if not settings or not settings.access_token:
            raise UserError(request.env._("Not authenticated with Razyyn."))

        previous = request.env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id), ("sender", "=", "ai")], order="id desc", limit=1
        )

        attachment_ids = self._attachment_ids(file_urls, session)

        # A pending plan is answered by this message before anything else reads
        # the transcript, so the card the customer just clicked stops being
        # answerable even if the turn below fails.
        self._settle_pending_plan(session, message)

        # THE CUSTOMER'S OWN WORDS ALWAYS GO INTO THEIR TRANSCRIPT. What is
        # stored for an answer is the ANSWER, not the envelope: the reply
        # arrives with the agent's full question wrapped around it, and echoing
        # that back at them at full length is why it used to be skipped.
        message_name = None
        if message == "Approve":
            pass
        elif message.startswith("Clarification Response:"):
            from ..services import transcript

            said = transcript._answer_text(message)
            if said and not turns.fold_the_answer_in(request.env, session, said):
                # A duplicated answer is cosmetic; a lost one is the complaint
                # this fallback exists for.
                turns.save_chat_history(request.env, session, "human", said)
        else:
            stored = turns.save_chat_history(request.env, session, "human", message)
            message_name = str(stored.id) if stored else None

        # A new turn is not the cancelled one. Cleared here, in the request that
        # asks for it, so a stop pressed on the previous message cannot end this.
        session.write({"cancel_requested": False})
        request.env.cr.commit()

        turns.start_turn(
            request.env.cr.dbname, request.env.uid, session.session_id,
            message, agent_type or "auto", attachment_ids, _as_bool(scan),
            _as_bool(high_thinking),
        )
        return _ok({
            "status": "queued",
            "session_id": session.session_id,
            "message_name": message_name,
            "previous_ai_message_name": str(previous.id) if previous else None,
        })

    def _settle_pending_plan(self, session, message) -> None:
        """Move a plan card from "pending" to what the customer just said."""
        plans = request.env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id), ("content", "=like", '{"type": "plan"%')],
            order="id desc", limit=1,
        )
        if not plans:
            return
        try:
            data = json.loads(plans.content)
        except (ValueError, TypeError):
            return
        if data.get("status") != "pending":
            return
        data["status"] = "approved" if message == "Approve" else "refused"
        plans.content = json.dumps(data, ensure_ascii=False)

    def _attachment_ids(self, file_urls, session) -> list:
        """The uploads named by this message, as ids, refusing anyone else's.

        The browser sends back what `upload_agent_file` gave it. Each is checked
        against this user's own uploads, so a crafted id cannot attach a
        colleague's document to a question.
        """
        if isinstance(file_urls, str):
            try:
                file_urls = json.loads(file_urls)
            except ValueError:
                file_urls = [file_urls]
        wanted = []
        for entry in file_urls or []:
            if isinstance(entry, dict):
                entry = entry.get("file_url") or entry.get("id")
            text = str(entry or "")
            digits = text.rsplit("/", 1)[-1]
            if digits.isdigit():
                wanted.append(int(digits))
        if not wanted:
            return []
        return request.env["ir.attachment"].sudo().search([
            ("id", "in", wanted),
            ("res_model", "=", "razyyn.agent.chat.session"),
            ("create_uid", "=", request.env.uid),
        ]).ids

    @http.route("/razyyn/api/edit_message", type="json", auth="user")
    def edit_message(self, session_id=None, message_name=None, message=None,
                     agent_email=None, agent_type="auto", file_urls=None,
                     scan=False, high_thinking=False, title=None, **_kwargs):
        """Edit one of your own messages and regenerate the answer from there.

        Everything said after the edited turn -- by the customer or the agent --
        is discarded from this transcript, and the edited turn is sent again as
        a brand new message. The agent gets a FRESH thread so its own state does
        not carry the discarded turns forward, while this conversation keeps its
        identity, its remaining history and its place in the sidebar.
        """
        session = _owned_session_or_refuse(session_id)
        if not message_name:
            raise UserError(request.env._("Message ID is required."))
        if not (message or "").strip():
            raise UserError(request.env._("Message cannot be empty."))

        row = request.env["razyyn.agent.chat.message"].sudo().browse(
            int(message_name) if str(message_name).isdigit() else 0
        ).exists()
        if not row or row.session_id.id != session.id:
            raise UserError(request.env._("Message not found in this chat."))
        if row.sender != "human":
            raise UserError(request.env._("Only your own messages can be edited."))
        if row.content == "Approve" or (row.content or "").startswith("Clarification Response:"):
            raise UserError(request.env._("This message can't be edited."))

        # Stop first: a run still answering the discarded branch must not race
        # the fresh one for the same conversation.
        try:
            self.cancel_agent(session_id=session.session_id, agent_email=agent_email)
        except Exception as exc:
            # Nothing in flight is the common case, not a failure.
            _logger.debug("Razyyn AI: cancel_agent before edit found nothing to stop: %s", exc)

        # The edited turn is sent again below as a brand new row, so it is
        # dropped here rather than rewritten -- one code path for "what a
        # message looks like once sent" instead of two.
        request.env["razyyn.agent.chat.message"].sudo().search([
            ("session_id", "=", session.id), ("id", ">=", row.id),
        ]).unlink()

        values = {"backend_session_id": str(uuid.uuid4())}
        if title:
            values["title"] = title
        session.write(values)
        request.env.cr.commit()

        return self.send_message(
            message=message, session_id=session.session_id, agent_email=agent_email,
            agent_type=agent_type, file_urls=file_urls, scan=scan,
            high_thinking=high_thinking,
        )

    @http.route("/razyyn/api/cancel_agent", type="json", auth="user")
    def cancel_agent(self, session_id=None, agent_email=None, **_kwargs):
        """Stop the turn, here and on the platform.

        Recorded before anybody is asked, because the turn may not have left
        this server yet -- a stop pressed in that window must still stop it.
        """
        session = _owned_session_or_refuse(session_id)
        session.write({"cancel_requested": True})
        request.env.cr.commit()

        settings = _mine(agent_email)
        if not settings:
            raise UserError(request.env._("Not authenticated with Razyyn."))

        def send(headers):
            headers = dict(headers, **{"Content-Type": "application/json"})
            return requests.post(
                f"{_server_url()}/agent/cancel",
                json={"session_id": session.get_backend_session_id()},
                headers=headers,
                timeout=turns.CANCEL_TIMEOUT,
            )

        try:
            response = turns.call_the_platform(request.env, settings, send)
        except (turns.SessionEnded, turns.PlatformUnreachable) as stopped:
            raise UserError(str(stopped)) from stopped

        if response.status_code != 200:
            raise UserError(self._detail(response, "The agent server refused the stop."))

        turns.save_chat_event_if_not_duplicate(
            request.env, session, "ai", turns.CANCELLED_TEXT
        )
        try:
            return _ok({"success": True, "message": response.json().get("message")})
        except ValueError:
            return _ok({"success": True})

    @http.route("/razyyn/api/get_run_state", type="json", auth="user")
    def get_run_state(self, session_id=None, agent_email=None, **_kwargs):
        """The manager's checklist for one conversation, for redrawing a reload.

        Degrades to "none" on any failure, because a missing checklist must not
        stop a customer opening their chat.
        """
        session = _session(session_id)
        settings = _mine(agent_email)
        if not session or not settings:
            return _ok({"status": "none", "tasks": []})

        def send(headers):
            return requests.get(
                f"{_server_url()}/agent/chat/state",
                params={"session_id": session.get_backend_session_id()},
                headers=headers,
                timeout=turns.STATE_TIMEOUT,
            )

        try:
            response = turns.call_the_platform(request.env, settings, send)
        except (turns.SessionEnded, turns.PlatformUnreachable):
            return _ok({"status": "none", "tasks": []})
        except requests.exceptions.RequestException as exc:
            _logger.warning("Razyyn AI: could not read the run state: %s", exc)
            return _ok({"status": "none", "tasks": []})

        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                return _ok(body)
        return _ok({"status": "none", "tasks": []})

    @http.route("/razyyn/api/get_turn_result", type="json", auth="user")
    def get_turn_result(self, session_id=None, previous_ai_message_name=None, **_kwargs):
        """A newly stored answer, for a browser that missed the live one.

        The worker writes the transcript before it publishes the terminal
        event. This inexpensive query is the durable fallback when the browser
        was not listening. Comparing row ids avoids replaying an older answer.
        """
        session = _session(session_id)
        if not session:
            return _ok({"status": "pending"})

        latest = request.env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id), ("sender", "=", "ai")], order="id desc", limit=1
        )
        if not latest or str(latest.id) == str(previous_ai_message_name or ""):
            return _ok({"status": "pending"})

        content = latest.content or ""
        if content == turns.CANCELLED_TEXT:
            status = "cancelled"
        elif content.startswith(turns.ERROR_PREFIX):
            status = "error"
        else:
            status = "completed"
        return _ok({"status": status, "message": _message_row(latest)})

    # ── Uploads ─────────────────────────────────────────────────────────────

    @http.route("/razyyn/api/get_upload_rules", type="json", auth="user")
    def get_upload_rules(self, **_kwargs):
        """What the server actually enforces, so the browser keeps no copy.

        The picker used to carry its own list and it had drifted: a customer
        chose a file, watched it upload, and was told afterwards it was not
        allowed -- the one moment a file picker exists to prevent.
        """
        return _ok({
            "extensions": list(ALLOWED_EXTENSIONS),
            "image_extensions": list(IMAGE_EXTENSIONS),
            "excel_extensions": list(EXCEL_EXTENSIONS),
            "max_files": MAX_UPLOAD_FILES,
            "max_size_mb": MAX_FILE_SIZE_BYTES // (1024 * 1024),
        })

    @http.route("/razyyn/api/upload_agent_file", type="http", auth="user",
                methods=["POST"], csrf=True)
    def upload_agent_file(self, **_kwargs):
        """Take one file and keep it against this conversation.

        A form post rather than JSON, because a file is multipart -- so this is
        the one route that carries a CSRF token, and Odoo checks it before the
        handler runs.
        """
        upload = request.httprequest.files.get("file")
        if not upload:
            return request.make_json_response(
                {"error": "No file was received."}, status=400
            )

        name = os.path.basename(upload.filename or "upload")
        extension = os.path.splitext(name.lower())[1]
        if extension not in ALLOWED_EXTENSIONS:
            return request.make_json_response(
                {"error": f"{extension or 'That kind of file'} cannot be attached here."},
                status=400,
            )

        data = upload.read()
        if len(data) > MAX_FILE_SIZE_BYTES:
            return request.make_json_response(
                {"error": f"That file is larger than {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB."},
                status=400,
            )

        attachment = request.env["ir.attachment"].sudo().create({
            "name": name,
            "raw": data,
            "res_model": "razyyn.agent.chat.session",
            "mimetype": upload.mimetype or None,
        })
        return request.make_json_response(_ok({
            "file_url": f"/razyyn/chat/download_file/{attachment.id}",
            "file_name": name,
            "file_size": len(data),
            "is_private": 1,
        }))

    @http.route("/razyyn/chat/download_file/<int:attachment_id>", type="http",
                auth="user", methods=["GET"])
    def download_file(self, attachment_id, **_kwargs):
        """Hand back a file this user uploaded, and nobody else's."""
        attachment = request.env["ir.attachment"].sudo().browse(attachment_id).exists()
        if (not attachment
                or attachment.res_model != "razyyn.agent.chat.session"
                or attachment.create_uid.id != request.env.uid):
            raise werkzeug.exceptions.NotFound()
        return request.make_response(
            attachment.raw,
            headers=[
                ("Content-Type", attachment.mimetype or "application/octet-stream"),
                ("Content-Disposition",
                 http.content_disposition(attachment.name or "download")),
            ],
        )

    @http.route("/razyyn/api/get_user_usage", type="json", auth="user")
    def get_user_usage(self, email=None, **_kwargs):
        """How much of this account's monthly allowance is spent.

        Scoped to the caller's own connection, always: usage is billing
        information, and an endpoint that accepted any e-mail address would
        report one customer's consumption to another.

        Never fails the page. A badge that cannot be drawn is a missing badge;
        it is not a reason the chat should not open.
        """
        zero = {"total_usage_percentage": 0.0, "plan": "free"}
        settings = _mine(email)
        if not settings or not settings.access_token:
            return _ok(zero)

        identifier = self._platform_user_id(settings)
        if not identifier:
            return _ok(zero)

        def send(headers):
            return requests.get(
                f"{_server_url()}/users/{identifier}/usage",
                headers=headers, timeout=BANNER_TIMEOUT,
            )

        try:
            response = turns.call_the_platform(request.env, settings, send)
            if response.status_code != 200:
                return _ok(zero)
            body = response.json()
        except Exception as exc:
            _logger.info("Razyyn AI: could not read plan usage: %s", exc)
            return _ok(zero)

        return _ok({
            "total_usage_percentage": round(body.get("total_usage_percentage", 0.0), 1),
            "plan": body.get("plan", "free"),
        })

    # ── Announcements ───────────────────────────────────────────────────────

    @http.route("/razyyn/api/get_active_banner_message", type="json", auth="user")
    def get_active_banner_message(self, **_kwargs):
        """Whatever the platform is currently telling every customer, if anything.

        Never fails the page: a notice that cannot be fetched is no notice.
        """
        settings = _settings()
        if not settings or not settings.access_token:
            return _ok({})

        def send(headers):
            # `/banners/active`, plural -- the same path the Frappe app calls.
            # The singular was a 404 on every page load, and because a notice
            # that cannot be fetched is treated as no notice, no customer on
            # Odoo has ever been shown a broadcast.
            return requests.get(f"{_server_url()}/banners/active",
                                headers=headers, timeout=BANNER_TIMEOUT)

        try:
            response = turns.call_the_platform(request.env, settings, send)
        except Exception:
            return _ok({})
        if response.status_code != 200:
            return _ok({})
        try:
            body = response.json()
        except ValueError:
            return _ok({})
        return _ok(body if isinstance(body, dict) else {})
