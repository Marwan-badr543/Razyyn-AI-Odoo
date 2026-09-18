# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""The company's accounting knowledge, and the jurisdiction its rules come from.

WHAT THIS IS FOR
    Two things the agent reads before it answers a question about how THIS
    business keeps its books: the company's own accounting policy, and the
    country whose requirements apply. Both live on the platform, because both
    are read by the agent's helpers at answer time -- there is nothing for Odoo
    to store and nothing for it to decide.

WHY THE PDF IS NEVER STORED HERE
    It is forwarded from the request stream straight to the platform, which
    extracts the searchable text and deletes its own copy. Writing it to an
    ``ir.attachment`` on the way through would put a copy of a customer's
    internal accounting policy in the Odoo filestore, where nothing would ever
    delete it and every backup would carry it. The Frappe app has the same rule
    for the same reason.

WHY THERE IS NO COUNTRY LIST IN THIS FILE
    ``/knowledge/countries`` publishes it. A second copy here would be the
    thing that goes stale, and the failure is invisible: a country missing from
    this list is simply one the customer cannot pick, with nothing anywhere
    saying why.
"""

from __future__ import annotations

import base64
import json
import logging

import requests

from .chat_turn_service import (
    PlatformUnreachable,
    SessionEnded,
    call_the_platform,
    server_url,
)

_logger = logging.getLogger(__name__)

#: Reading a forty-megabyte PDF and indexing it is real work on the platform's
#: side. Generous, because the alternative to waiting is a timeout that looks
#: to the customer exactly like a refusal.
_TIMEOUT = (10, 120)

#: What the platform accepts. Checked here as well as in the browser so a
#: request built by hand cannot spend two minutes uploading something that was
#: always going to be refused.
MAX_PDF_BYTES = 40 * 1024 * 1024


class KnowledgeRefused(Exception):
    """The platform declined, and said why. Its sentence is the message."""


def _settings(env, uid):
    settings = env["razyyn.agent.settings"].sudo().search(
        [("user_id", "=", uid)], limit=1,
    )
    if not settings or not settings.access_token:
        raise SessionEnded(
            "Sign in to Razyyn AI from the chat page first, then try again."
        )
    return settings


def _reason(response) -> str:
    """The platform's own sentence, never a generic one.

    Every failure used to read the same on the Frappe card, and that is what
    made it useless: "this PDF has no searchable text", "larger than 40 MB" and
    "your session expired" are three different things for the customer to do.
    """
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:400]
    detail = body.get("detail") or body.get("message") or body.get("error")
    if isinstance(detail, list):
        return "; ".join(str(item.get("msg") or item) for item in detail[:3])
    return str(detail or "").strip()


def _call(env, uid, method: str, path: str, build) -> requests.Response:
    """One authenticated request to the platform, renewed once if stale.

    ``build`` is called per attempt and returns the kwargs for this request, so
    a retry after a 401 sends a FRESH upload body. Handing `requests` an
    already-read stream twice posts nothing the second time, and the customer
    is told their forty-megabyte policy contains no text.
    """
    settings = _settings(env, uid)
    url = f"{server_url(env)}{path}"

    def send(headers):
        return requests.request(
            method, url, headers=headers, timeout=_TIMEOUT, **build(),
        )

    try:
        response = call_the_platform(env, settings, send)
    except PlatformUnreachable as exc:
        _logger.warning("Razyyn AI: knowledge request failed: %s", exc)
        raise KnowledgeRefused(
            "Could not reach Razyyn AI. Check the connection and try again."
        ) from exc

    if response.status_code >= 400:
        raise KnowledgeRefused(_reason(response) or "The request was refused.")
    return response


# ── What the card shows ──────────────────────────────────────────────────────


def catalogue(env, uid) -> dict:
    """The policy in use, the chosen country, and the countries on offer."""
    return _call(env, uid, "GET", "/knowledge", lambda: {}).json()


def upload_policy(env, uid, filename: str, content: bytes,
                  title: str = "Company accounting policy") -> dict:
    """Forward one searchable-text PDF. Nothing is kept on this side."""
    if not filename.lower().endswith(".pdf"):
        raise KnowledgeRefused(
            "That is not a PDF. Export your policy as a PDF and try again."
        )
    if not content:
        raise KnowledgeRefused("That file is empty.")
    if len(content) > MAX_PDF_BYTES:
        raise KnowledgeRefused(
            f"That PDF is {len(content) / 1048576:.1f} MB. The limit is 40 MB."
        )

    def build():
        # Rebuilt per attempt: see _call.
        return {
            "data": {"title": title or "Company accounting policy"},
            "files": {"file": (filename, content, "application/pdf")},
        }

    return _call(env, uid, "POST", "/knowledge/company", build).json()


def delete_policy(env, uid, document_id: str) -> None:
    if not document_id:
        raise KnowledgeRefused("No document was named.")
    _call(env, uid, "DELETE", f"/knowledge/company/{document_id}", lambda: {})


def set_country(env, uid, country_code: str) -> dict:
    """Set the jurisdiction whose accounting requirements the agent applies."""
    code = str(country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise KnowledgeRefused("Choose a country from the list.")

    settings = _settings(env, uid)
    user_id = _account_id(settings.sudo().access_token)
    if not user_id:
        raise KnowledgeRefused(
            "Could not identify the connected Razyyn account. Sign in again "
            "from the chat page."
        )
    return _call(
        env, uid, "PATCH", f"/users/{user_id}",
        lambda: {"json": {"country_code": code}},
    ).json()


def _account_id(token: str) -> str:
    """The platform account this Odoo is signed in as, read from the token.

    The country belongs to the ACCOUNT, not to this connection, so the route
    that sets it is addressed by account id -- and the only place this side
    holds that id is the `sub` claim of the token it was issued. Read, never
    verified: the platform verifies the signature, and a token this side
    tampered with would simply be refused there.
    """
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except Exception:
        return ""
    return str(claims.get("sub") or "")
