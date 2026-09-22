# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""One authenticated request to the Razyyn platform, as the signed-in account.

WHY THIS IS ITS OWN MODULE
    Three things are needed by everything in this Odoo that asks the platform a
    question about the customer's own account: which connection is signed in,
    which platform account that connection belongs to, and how to send one
    request as it and renew a stale token once.

    They were written inside `knowledge_service` because the company-knowledge
    card needed them first. The usage card needs exactly the same three, and the
    choice at that point is to reach into another module's private names or to
    put the plumbing where both can see it. They are here, so neither card owns
    them and a third one does not have to pick a module to borrow from.

WHAT IS DELIBERATELY NOT HERE
    Anything about what is being asked. This module knows how to be
    authenticated; every caller knows what it wants.
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

#: Generous on read, very generous on write. Indexing a forty-megabyte PDF is
#: real work on the platform's side, and the alternative to waiting is a timeout
#: that looks to the customer exactly like a refusal.
TIMEOUT = (10, 120)


class PlatformRefused(Exception):
    """The platform declined, and said why. Its own sentence is the message.

    NEVER REPLACED WITH A GENERIC ONE. "This PDF has no searchable text",
    "larger than 40 MB" and "your session expired" are three different things
    for the customer to do, and every card in this module shows this text
    verbatim for that reason.
    """


def settings_of(env, uid):
    """The signed-in connection for this Odoo user, or a session that has ended."""
    settings = env["razyyn.agent.settings"].sudo().search(
        [("user_id", "=", uid)], limit=1,
    )
    if not settings or not settings.access_token:
        raise SessionEnded(
            "Sign in to Razyyn AI from the chat page first, then try again."
        )
    return settings


def account_id_of(token: str) -> str:
    """The platform account a token was issued to, read from its `sub` claim.

    Read, never verified: the platform verifies its own signatures, and a token
    this side tampered with would simply be refused there. This is the only
    place this side holds that id, and the routes addressed by account — the
    country, the usage figure — need it.
    """
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except Exception:
        return ""
    return str(claims.get("sub") or "")


def reason(response) -> str:
    """The platform's own sentence for a refusal, never a generic one."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:400]
    detail = body.get("detail") or body.get("message") or body.get("error")
    if isinstance(detail, list):
        return "; ".join(str(item.get("msg") or item) for item in detail[:3])
    return str(detail or "").strip()


def request_as_account(env, uid, method: str, path: str, build) -> requests.Response:
    """One authenticated request to the platform, renewed once if stale.

    ``build`` is called PER ATTEMPT and returns the kwargs for that request, so
    a retry after a 401 sends a FRESH body. Handing `requests` an already-read
    stream twice posts nothing the second time, and the customer is told their
    forty-megabyte policy contains no text.
    """
    settings = settings_of(env, uid)
    url = f"{server_url(env)}{path}"

    def send(headers):
        return requests.request(
            method, url, headers=headers, timeout=TIMEOUT, **build(),
        )

    try:
        response = call_the_platform(env, settings, send)
    except PlatformUnreachable as exc:
        _logger.warning("Razyyn AI: platform request failed: %s", exc)
        raise PlatformRefused(
            "Could not reach Razyyn AI. Check the connection and try again."
        ) from exc

    if response.status_code >= 400:
        raise PlatformRefused(reason(response) or "The request was refused.")
    return response
