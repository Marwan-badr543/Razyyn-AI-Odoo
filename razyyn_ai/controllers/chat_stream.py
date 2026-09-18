# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""The chat page itself, and the live stream that keeps it up to date.

WHAT REPLACES SOCKET.IO
    On ERPNext the chat window is fed by Frappe's realtime socket. Odoo has a
    bus of its own, but this page is served outside the web client -- so what
    it gets instead is an EventSource against /razyyn/chat/events, which is
    plain HTTP and needs nothing installed.

    The difference that matters is not the transport, it is that this one
    REPLAYS ACROSS A RECONNECT. Every event was written to
    razyyn.agent.chat.event before it was sent, and the browser hands back the
    last id it saw -- so the fifty-second recycle below, a blinking proxy and a
    sleeping laptop all cost nothing.

    A RELOAD IS A DIFFERENT CASE and is not served from here: a fresh page has
    no marker, so `_events` starts it at the newest row rather than replaying a
    finished run over a new one. What recovers a reload is the transcript and
    the agent's own run state, not this table.

WHY THE STREAM ENDS ITSELF
    An open connection holds one Odoo HTTP worker for as long as it lives. Half
    a dozen chat windows left open overnight would take the pool and the site
    would stop answering -- for everyone, not just for chat users. So the
    server ends the connection after a bounded spell and the browser reopens it
    with its last id. Nothing is lost, because the events are in a table.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time

import markupsafe

import odoo
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

#: How long one connection is allowed to live before the browser is asked to
#: reopen it. Comfortably inside Odoo's own request time limits.
STREAM_SECONDS = 50

#: How often the table is asked for new events. Small enough that a token
#: arrives as it is written; large enough that an idle chat is a query a
#: second, which a site can afford.
POLL_SECONDS = 0.5

#: Sent when there is nothing to say, so a proxy in the middle does not decide
#: the connection is dead and close it.
KEEPALIVE = ": keep-alive\n\n"


#: THE CHAT PAGE'S ASSETS ARE NOT IN AN ODOO BUNDLE, SO NOTHING FINGERPRINTS
#: THEM FOR US.
#:
#: Odoo serves `/<module>/static/...` with `Cache-Control: public,
#: max-age=604800`. A browser that opened the chat once keeps those bytes for a
#: week, whatever the server now holds. The four URLs therefore carry a cache
#: key -- and for six module versions that key was the string "1.2.0", typed
#: into the template beside a comment asking whoever changed the assets to
#: change it too. Nobody did. Customers on 1.7.0 were running the chat window
#: from 1.2.0: no High Thinking switch, and a thinking badge that still said
#: "Router" months after it was renamed.
#:
#: A cache key a person has to remember to change is a cache key that is wrong.
#: This one is the SHA-256 of the asset bytes themselves, so it changes when
#: and only when the assets change -- including on a `git pull` that bumps no
#: version, and including in --dev mode where an edited file must be picked up
#: at once.
_ASSET_FILES = (
    "static/src/chat/frappe/agent_chat.css",
    "static/src/chat/razyyn_platform.css",
    "static/src/chat/razyyn_platform.js",
    "static/src/chat/frappe/agent_chat.js",
)

#: (token, signature) -- recomputed only when a file's size or mtime moves.
#: Hashing 216 KB on every page load would be a needless read; stat-ing four
#: files is not. The signature catches an edit the version number would miss.
_asset_token_cache: tuple[str, tuple] | None = None


def chat_assets_token() -> str:
    """The cache key the chat page's four asset URLs carry.

    Derived from the files' own bytes, so it cannot fall behind them.
    """
    global _asset_token_cache

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = [os.path.join(here, name) for name in _ASSET_FILES]
    try:
        signature = tuple(
            (os.path.getsize(p), os.path.getmtime(p)) for p in paths
        )
    except OSError:
        # A missing asset is a broken install, not a caching question. Fall
        # back to the module version so the page still renders and the error
        # the customer sees is the real one (a 404 on the asset).
        return _module_version()

    if _asset_token_cache is not None and _asset_token_cache[1] == signature:
        return _asset_token_cache[0]

    digest = hashlib.sha256()
    for path in paths:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
    token = digest.hexdigest()[:16]
    _asset_token_cache = (token, signature)
    return token


def _module_version() -> str:
    """The installed module's version, used only when an asset is missing."""
    module = request.env["ir.module.module"].sudo().search(
        [("name", "=", "razyyn_ai")], limit=1,
    )
    return (module.latest_version or "0").replace(".", "-")


class RazyynChatStream(http.Controller):

    @http.route("/razyyn/chat", type="http", auth="user", methods=["GET"])
    def chat_page(self, **_kwargs):
        """The chat window.

        Everything it needs to start is rendered into the page as JSON --
        nothing here is interpolated into JavaScript by hand.
        """
        user = request.env.user
        boot = {
            "user": user.login,
            "lang": (user.lang or "en_US")[:2],
            "csrf_token": request.csrf_token(),
        }
        response = request.render("razyyn_ai.chat_page", {
            # Printed into a <script> block, where the browser does no
            # entity decoding -- so it goes out raw, with "<" escaped
            # instead so the block cannot be closed from inside a value.
            "boot_json": markupsafe.Markup(json.dumps(boot).replace("<", "\\u003c")),
            "lang": (user.lang or "en_US").replace("_", "-"),
            "direction": "rtl" if (user.lang or "").startswith("ar") else "ltr",
            # See chat_assets_token: the cache key for the four standalone
            # assets, derived from their bytes so it can never fall behind them.
            "assets_token": chat_assets_token(),
        })
        # The page is embedded in Odoo's own client action, so it must be
        # frameable -- but only by this site. Without this it is frameable by
        # anyone, and a chat window showing a customer's ledger is worth
        # clickjacking.
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @http.route("/razyyn/chat/events", type="http", auth="user", methods=["GET"])
    def events(self, last_id=None, **_kwargs):
        """Everything that has happened in this user's runs since `last_id`.

        The browser's own `Last-Event-ID` header wins over the query string:
        EventSource sends it automatically on every reconnect, which is exactly
        the case that must not lose anything.
        """
        dbname = request.env.cr.dbname
        uid = request.env.uid
        resume = request.httprequest.headers.get("Last-Event-ID") or last_id
        try:
            cursor_id = int(resume)
        except (TypeError, ValueError):
            cursor_id = None

        response = request.make_response(
            _events(dbname, uid, cursor_id),
            headers=[
                ("Content-Type", "text/event-stream"),
                ("Cache-Control", "no-cache, no-transform"),
                ("Connection", "keep-alive"),
                # nginx buffers a response by default, which for a stream means
                # the customer sees nothing until the turn is over.
                ("X-Accel-Buffering", "no"),
            ],
        )
        return response


def _events(dbname: str, uid: int, cursor_id: int | None):
    """Yield this user's events as they are written, until the spell is up.

    A NEW CURSOR PER POLL, DELIBERATELY. The turn is running in a different
    process, and a transaction that began before it committed cannot see what
    it wrote no matter how long it waits. This connection's job is to see
    other people's commits, so it starts a fresh transaction each time it looks.
    """
    started = time.monotonic()

    # Where to resume from when the browser has never seen anything: the events
    # already in the table belong to turns that finished before this page
    # opened, and replaying them would redraw an old run over a new one.
    if cursor_id is None:
        cursor_id = _latest_event_id(dbname, uid)

    # Tells EventSource how long to wait before reopening. The stream ends
    # every STREAM_SECONDS on purpose, so this is the gap between them.
    yield "retry: 1000\n\n"

    while time.monotonic() - started < STREAM_SECONDS:
        rows = _read_new(dbname, uid, cursor_id)
        for row_id, event, payload in rows:
            cursor_id = row_id
            body = json.dumps({"event": event, "message": json.loads(payload or "{}")})
            yield f"id: {row_id}\nevent: razyyn\ndata: {body}\n\n"
        if not rows:
            yield KEEPALIVE
        # THE PAUSE IS NOT OPTIONAL, AND IT IS NOT ONLY FOR AN IDLE CHAT.
        #
        # It used to be skipped whenever the last look found something, which
        # is precisely what happens all the way through an answer being
        # written. The loop then spun as fast as the database could answer it —
        # hundreds of queries a second, one processor core held for the whole
        # of every turn, times the number of conversations running. Two at once
        # was enough to slow the whole site down, which is what the chat window
        # was showing when it stopped responding.
        #
        # Half a second behind is invisible in a chat and costs two queries a
        # second per open window.
        time.sleep(POLL_SECONDS)


def _latest_event_id(dbname: str, uid: int) -> int:
    with odoo.registry(dbname).cursor() as cr:
        cr.execute(
            "SELECT COALESCE(MAX(id), 0) FROM razyyn_agent_chat_event WHERE user_id = %s",
            (uid,),
        )
        return cr.fetchone()[0]


def _read_new(dbname: str, uid: int, after: int) -> list:
    """The next events for this user, oldest first.

    Read with SQL rather than through the ORM: this runs twice a second for
    every open chat window, and the ORM's own caching is worse than useless
    here -- it would answer from a snapshot of a transaction that has ended.
    """
    try:
        with odoo.registry(dbname).cursor() as cr:
            cr.execute(
                """
                SELECT id, event, payload
                  FROM razyyn_agent_chat_event
                 WHERE user_id = %s AND id > %s
                 ORDER BY id ASC
                 LIMIT 200
                """,
                (uid, after or 0),
            )
            return cr.fetchall()
    except Exception as exc:  # pragma: no cover - a dropped poll must not end the stream
        _logger.warning("Razyyn AI: could not read chat events: %s", exc)
        return []
