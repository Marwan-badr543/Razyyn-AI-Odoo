# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""The company-knowledge card's four calls, from this Odoo to the platform.

WHY A CONTROLLER AND NOT A MODEL METHOD
    One of the four carries a PDF, and it must reach the platform without being
    written down here. An Odoo binary field -- on a record or on a wizard --
    puts a copy of the customer's internal accounting policy in the filestore
    or the database, where nothing deletes it and every backup keeps it. A
    controller can take the upload straight off the request and hand the bytes
    on, which is what the Frappe app does and for the same reason.

    The other three carry nothing but a word, and they are plain HTTP too, so
    all four answer in one shape. They were ``type="json"``, which meant the
    card had to reach them through Odoo's ``rpc`` service -- and that service
    was REMOVED in Odoo 18, so the card rendered on 17 and threw "Service rpc
    is not available" on 18. One product, two framework generations: the fewer
    framework services the shared code touches, the fewer ways it can diverge.
    Reads are GET, writes are POST with Odoo's CSRF token.
"""

from __future__ import annotations

import json
import logging

from odoo import http
from odoo.http import request

from ..services import knowledge_service
from ..services.chat_turn_service import SessionEnded

_logger = logging.getLogger(__name__)


def _refused(exc: Exception) -> dict:
    """A refusal shaped the same whichever layer produced it.

    The card shows `error` verbatim, so whatever is put here is what the
    customer reads. It is always the platform's own sentence -- "this PDF has
    no searchable text", "larger than 40 MB", "your session has ended" -- never
    a generic one, because those three need three different actions.
    """
    return {"ok": False, "error": str(exc)}


class RazyynKnowledge(http.Controller):

    @http.route("/razyyn/knowledge/catalogue", type="http", auth="user",
                methods=["GET"], csrf=False)
    def catalogue(self, **_kwargs):
        try:
            return _json({
                "ok": True,
                **knowledge_service.catalogue(request.env, request.env.uid),
            })
        except (knowledge_service.KnowledgeRefused, SessionEnded) as exc:
            return _json(_refused(exc))

    @http.route("/razyyn/knowledge/country", type="http", auth="user",
                methods=["POST"], csrf=True)
    def country(self, country_code=None, **_kwargs):
        try:
            knowledge_service.set_country(request.env, request.env.uid, country_code)
            return _json({"ok": True})
        except (knowledge_service.KnowledgeRefused, SessionEnded) as exc:
            return _json(_refused(exc))

    @http.route("/razyyn/knowledge/policy/delete", type="http", auth="user",
                methods=["POST"], csrf=True)
    def delete_policy(self, document_id=None, **_kwargs):
        try:
            knowledge_service.delete_policy(request.env, request.env.uid, document_id)
            return _json({"ok": True})
        except (knowledge_service.KnowledgeRefused, SessionEnded) as exc:
            return _json(_refused(exc))

    @http.route("/razyyn/knowledge/policy", type="http", auth="user",
                methods=["POST"], csrf=True)
    def upload_policy(self, **kwargs):
        """Forward one PDF. The bytes are read, sent, and dropped."""
        upload = request.httprequest.files.get("file")
        if upload is None:
            return _json(_refused(ValueError("Choose a PDF first.")))
        try:
            indexed = knowledge_service.upload_policy(
                request.env, request.env.uid,
                upload.filename or "policy.pdf",
                upload.read(),
                kwargs.get("title") or "Company accounting policy",
            )
            return _json({"ok": True, **indexed})
        except (knowledge_service.KnowledgeRefused, SessionEnded) as exc:
            return _json(_refused(exc))
        except Exception as exc:  # noqa: BLE001 - the customer gets a sentence
            _logger.exception("Razyyn AI: company policy upload failed")
            return _json(_refused(exc))


def _json(body: dict):
    return request.make_response(
        json.dumps(body),
        headers=[("Content-Type", "application/json"), ("Cache-Control", "no-store")],
    )
