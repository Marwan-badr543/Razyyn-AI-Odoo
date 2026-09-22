# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""How much of the plan's monthly budget this account has spent.

WHY THIS IS READ LIVE AND NEVER STORED
    It is a figure the platform owns: it changes with every request the agent
    answers, on any of the customer's devices, and it is the basis of a bill. A
    copy in this Odoo would be a second answer to the same question, stale by
    however long ago it was written, and the moment it disagreed the customer
    would be reading the wrong one — on the one card whose whole job is to warn
    them before their next request is refused.

WHY A FAILURE IS NOT AN ERROR HERE
    The Frappe app settled this, and it is worth stating rather than repeating
    by accident: a widget that gives up shows the customer a red box about their
    balance whenever the platform is merely busy -- which is exactly when they
    came to look at it. So an unreachable platform reports a zeroed figure, the
    card says it could not read it, and nothing about the settings screen breaks.
"""

from __future__ import annotations

import logging

from .chat_turn_service import SessionEnded
from .platform_account import (
    PlatformRefused,
    account_id_of,
    request_as_account,
    settings_of,
)

_logger = logging.getLogger(__name__)

#: What an unreadable usage figure looks like. The same shape a real answer has,
#: so the card renders one way and never branches on "did this work".
UNKNOWN: dict = {"total_usage_percentage": 0.0, "plan": "free", "known": False}


def usage(env, uid) -> dict:
    """The plan and the share of its monthly budget spent, for this Odoo's account.

    Scoped to the signed-in connection's own account, read from the `sub` claim
    of the token this Odoo holds: usage is billing information, and a figure
    addressed by anything the caller could choose would report one customer's
    consumption to another.
    """
    settings = settings_of(env, uid)
    account_id = account_id_of(settings.sudo().access_token)
    if not account_id:
        raise PlatformRefused(
            "Could not identify the connected Razyyn account. Sign in again "
            "from the chat page."
        )

    body = request_as_account(env, uid, "GET", f"/users/{account_id}/usage", lambda: {}).json()
    return {
        # Rounded here rather than in the browser so both products round the
        # same way; a percentage is read, not calculated with.
        "total_usage_percentage": round(float(body.get("total_usage_percentage") or 0.0), 1),
        "plan": str(body.get("plan") or "free"),
        "known": True,
    }


def usage_or_unknown(env, uid) -> dict:
    """`usage`, with every failure turned into the zeroed figure.

    The card calls this one. `usage` stays separate because a caller that DOES
    want to know the reason -- a test, a future diagnostic -- must still be able
    to see it rather than a silent zero.
    """
    try:
        return usage(env, uid)
    except (PlatformRefused, SessionEnded) as exc:
        _logger.info("Razyyn AI: usage not read: %s", exc)
        return {**UNKNOWN, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the settings screen must still render
        _logger.exception("Razyyn AI: usage could not be read")
        return {**UNKNOWN, "error": str(exc)}
