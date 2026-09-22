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

import logging

from .platform_account import (
    PlatformRefused,
    account_id_of,
    request_as_account,
    settings_of,
)

_logger = logging.getLogger(__name__)

#: What the platform accepts. Checked here as well as in the browser so a
#: request built by hand cannot spend two minutes uploading something that was
#: always going to be refused.
MAX_PDF_BYTES = 40 * 1024 * 1024

#: The platform declined and said why.
#:
#: ONE TYPE UNDER TWO NAMES, ON PURPOSE. The refusal is the platform's, not this
#: card's -- `platform_account` raises it for the usage card too -- so there is
#: one class. This name is kept because the controllers catch by it, and a second
#: class would be a refusal that walked past every handler in this module and
#: ended the customer's request on a 500.
KnowledgeRefused = PlatformRefused


# ── What the card shows ──────────────────────────────────────────────────────


def catalogue(env, uid) -> dict:
    """The policy in use, the chosen country, and the countries on offer."""
    return request_as_account(env, uid, "GET", "/knowledge", lambda: {}).json()


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
        # Rebuilt per attempt: see `request_as_account`.
        return {
            "data": {"title": title or "Company accounting policy"},
            "files": {"file": (filename, content, "application/pdf")},
        }

    return request_as_account(env, uid, "POST", "/knowledge/company", build).json()


def delete_policy(env, uid, document_id: str) -> None:
    if not document_id:
        raise KnowledgeRefused("No document was named.")
    request_as_account(env, uid, "DELETE", f"/knowledge/company/{document_id}", lambda: {})


def set_country(env, uid, country_code: str) -> dict:
    """Set the jurisdiction whose accounting requirements the agent applies."""
    code = str(country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise KnowledgeRefused("Choose a country from the list.")

    settings = settings_of(env, uid)
    user_id = account_id_of(settings.sudo().access_token)
    if not user_id:
        raise KnowledgeRefused(
            "Could not identify the connected Razyyn account. Sign in again "
            "from the chat page."
        )
    return request_as_account(
        env, uid, "PATCH", f"/users/{user_id}",
        lambda: {"json": {"country_code": code}},
    ).json()
