# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Running one customer turn, and telling the chat window about it as it goes.

This is the Odoo half of what the Frappe app does in
`process_agent_message_background`: take the customer's message, call the agent
server, and turn the stream that comes back into the events the chat window
draws -- while writing the transcript down as it goes.

WHY IT IS A BACKGROUND THREAD AND NOT THE REQUEST
    The previous version streamed the agent's answer straight back down the
    customer's own HTTP request. That is simpler, and it has one behaviour
    nobody would accept if it were described out loud: closing the tab
    abandons the work. An accountant who starts a six-minute audit, switches to
    their inbox, and comes back finds nothing -- no answer, no transcript, and
    no sign that anything ever ran. The ledger writes the agent had already
    made are still there, which is worse than losing the work outright.

    So the turn is owned by a worker, exactly as on ERPNext. The browser is a
    spectator: it can leave, come back, or be replaced by a different browser
    on a different machine, and the turn carries on.

WHAT IT MAY NOT DO
    It may not decide what the customer sees. Which text is prose and which is
    an envelope, how a question is folded into the transcript, what a plan
    looks like -- all of that is in services/transcript.py, copied from the
    Frappe app so that both products answer identically. This file is the
    plumbing around it.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time

import requests

import odoo
from odoo import api, fields, SUPERUSER_ID

from . import ocr_service
from .held_words import HeldWords
from . import transcript
from .server_config import server_url as configured_server_url

_logger = logging.getLogger(__name__)

# ── Limits, matching the Frappe app's ───────────────────────────────────────
MAX_HISTORY_MESSAGES = 400
MAX_HISTORY_CHARS = 400_000
MAX_HISTORY_CHARS_PER_MESSAGE = 20_000
MAX_HISTORY_PAGE_SIZE = 200

#: Reaching the server is quick or it is broken; producing an answer is slow on
#: purpose. One number for both would either abandon a long audit or make a
#: dead server look like a slow one.
CONNECT_TIMEOUT_SECONDS = 30
TURN_TIMEOUT_SECONDS = 3 * 60 * 60
STREAM_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, TURN_TIMEOUT_SECONDS)
CANCEL_TIMEOUT = (10, 30)
STATE_TIMEOUT = (10, 20)

CANCELLED_TEXT = "⚠️ **Cancelled**"
ERROR_PREFIX = "⚠️ **Error:**"

# ── Telling the browser ─────────────────────────────────────────────────────
def publish(env, user_id: int, session_id: str, event: str, message: dict) -> None:
    """Write one event down, then let the stream carry it.

    Committed immediately and on its own. The chat window is watching a
    long-running job: an event held back until the end of the turn is not
    progress, it is a transcript delivered late. Never raises -- a turn must not
    die because its narration could not be written.
    """
    try:
        env["razyyn.agent.chat.event"].sudo().create({
            "user_id": user_id,
            "session_id": session_id,
            "event": event,
            "payload": json.dumps(message, ensure_ascii=False, default=str),
        })
        env.cr.commit()
    except Exception as exc:  # pragma: no cover - narration must never be fatal
        _logger.warning("Razyyn AI: could not publish %s: %s", event, exc)
        try:
            env.cr.rollback()
        except Exception as rollback_exc:
            _logger.debug("Razyyn AI: rollback after publish failure also failed: %s", rollback_exc)


#: How long the turn's events are left in place after the answer has been
#: delivered, before the turn sweeps up after itself.
#:
#: NOT ZERO, AND THE REASON IS THE LAST SENTENCE OF EVERY ANSWER. The chat
#: window does not read a row the instant it is written -- it looks twice a
#: second, and the connection is closed and reopened on purpose while a turn
#: runs. So there is always a short moment in which a row exists and has not
#: been collected yet. Delete inside that moment and the customer's screen
#: stops mid-sentence and hangs until the recovery poll rebuilds it from the
#: transcript: a second and a half with the tab in front, TEN SECONDS with it
#: in the background, landing exactly when they are waiting hardest.
#:
#: A few seconds would be enough. A minute costs nothing and covers a laptop
#: that slept through the end of the answer as well.
EVENT_CLEANUP_DELAY_SECONDS = 60


def highest_event_id(env, session_id: str) -> int:
    """The last row this conversation has written, as the database has it.

    Read in SQL because `publish` commits each row on this very cursor, and the
    ORM's cache is a snapshot taken before those commits.
    """
    try:
        env.cr.execute(
            "SELECT COALESCE(MAX(id), 0) FROM razyyn_agent_chat_event WHERE session_id = %s",
            (session_id,),
        )
        return env.cr.fetchone()[0]
    except Exception as exc:  # pragma: no cover - cleanup must never fail a turn
        _logger.warning("Razyyn AI: could not read the event high-water mark: %s", exc)
        return 0


def schedule_event_cleanup(dbname: str, session_id: str, high_water_mark: int) -> None:
    """Sweep this turn's events away a minute after it ends.

    BOUNDED BY ROW ID, NEVER BY CONVERSATION, and that is not a detail.
    Sweeping "this conversation's events" would mean: the answer lands, the
    customer immediately types a follow-up, and a minute later THIS turn's
    cleanup deletes the NEXT turn's rows while they are still being read. The
    second answer would stop dead mid-sentence -- rarely, unreproducibly, and
    only for the customers who type fastest.

    A later turn's rows are given higher ids by the sequence, so a ceiling
    taken at the moment this turn ended cannot reach them however the timing
    falls out.
    """
    if not high_water_mark:
        return
    thread = threading.Thread(
        target=_sweep_events,
        args=(dbname, session_id, high_water_mark),
        name=f"razyyn-sweep-{session_id[:12]}",
        daemon=True,
    )
    thread.start()


def sweep_events(cr, session_id: str, high_water_mark: int) -> int:
    """Remove one conversation's events up to `high_water_mark`. Returns how many.

    Takes a cursor rather than finding its own, so that the statement the tests
    exercise is the statement that runs in production -- a sweep that is only
    ever tested through a mock is a sweep whose WHERE clause nobody has checked.

    SQL rather than search().unlink(): this is a delivery buffer with nothing
    hanging off it -- no cascade, no override, nothing for the ORM to do but
    build thousands of records in order to throw them away again.
    """
    cr.execute(
        "DELETE FROM razyyn_agent_chat_event WHERE session_id = %s AND id <= %s",
        (session_id, high_water_mark),
    )
    return cr.rowcount


def _sweep_events(dbname: str, session_id: str, high_water_mark: int) -> None:
    """Wait out the delivery window, then remove what this turn wrote."""
    time.sleep(EVENT_CLEANUP_DELAY_SECONDS)
    try:
        with odoo.registry(dbname).cursor() as cr:
            removed = sweep_events(cr, session_id, high_water_mark)
            # pylint: disable=invalid-commit
            # This runs on its own cursor in a background thread (see
            # `thread.start()` above), outside any request lifecycle — there is
            # no dispatcher to commit on our behalf, so this explicit commit is
            # required, not a mid-request transaction violation.
            cr.commit()
        if removed:
            _logger.info("Razyyn AI: swept %s delivered event(s) from session %s",
                         removed, session_id)
    except Exception as exc:  # pragma: no cover - the hourly sweep is the backstop
        _logger.warning("Razyyn AI: could not sweep the events of %s: %s", session_id, exc)


# ── The transcript ──────────────────────────────────────────────────────────
def _session_of(env, session_id: str, user_id: int):
    return env["razyyn.agent.chat.session"].sudo().search(
        [("session_id", "=", session_id), ("user_id", "=", user_id)], limit=1
    )


def deprecate_previous_plans(env, session) -> None:
    """Mark every still-open plan in this session as superseded.

    A plan card the customer already answered must not stay answerable. The
    filter is done in SQL rather than by reading the whole transcript: on a long
    session that is the difference between four rows and four thousand.
    """
    try:
        plans = env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id), ("content", "=like", '{"type": "plan"%')],
            order="id desc", limit=50,
        )
        for plan in plans:
            try:
                data = json.loads(plan.content)
            except (ValueError, TypeError):
                continue
            if data.get("status") in ("pending", "refused", "approved"):
                data["status"] = "deprecated"
                plan.content = json.dumps(data, ensure_ascii=False)
    except Exception as exc:
        _logger.warning("Razyyn AI: could not deprecate earlier plans: %s", exc)


def save_chat_history(env, session, sender: str, content: str):
    """Store one message. Returns the record, or None if it could not be stored."""
    try:
        if content and content.strip().startswith('{"type": "plan"'):
            deprecate_previous_plans(env, session)
        message = env["razyyn.agent.chat.message"].sudo().create({
            "session_id": session.id,
            "sender": sender,
            "content": content,
        })
        session.sudo().write({"last_update": fields.Datetime.now()})
        env.cr.commit()
        return message
    except Exception as exc:
        _logger.error("Razyyn AI: could not save a chat message: %s", exc)
        try:
            env.cr.rollback()
        except Exception as rollback_exc:
            _logger.debug("Razyyn AI: rollback after save failure also failed: %s", rollback_exc)
        return None


def save_chat_event_if_not_duplicate(env, session, sender: str, content: str) -> None:
    """Store an event line unless it is already the last thing in the session."""
    try:
        last = env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id)], order="id desc", limit=1
        )
        if last and last.content == content:
            return
        save_chat_history(env, session, sender, content)
    except Exception as exc:
        _logger.warning("Razyyn AI: could not save a chat event: %s", exc)


def build_history_payload(env, session) -> str:
    """The conversation so far, as the JSON transcript the agent server expects.

    Bounded by SIZE, with a count only as a backstop, and spent newest-first --
    the turn that explains what the customer is referring to is far more often
    the last one than the first. Each message is clipped on its own so one
    pasted ledger cannot silence the twenty turns around it.

    Never raises: a history that cannot be read degrades this turn to a
    context-free one rather than failing the customer's message outright.
    """
    if not session:
        return ""
    try:
        recent = env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id)], order="id desc", limit=MAX_HISTORY_MESSAGES
        )
    except Exception as exc:
        _logger.warning("Razyyn AI: could not load history: %s", exc)
        return ""

    lines = []
    spent = 0
    for row in recent:
        content = transcript._prose_only(row.content)
        if not content:
            continue
        if len(content) > MAX_HISTORY_CHARS_PER_MESSAGE:
            content = content[:MAX_HISTORY_CHARS_PER_MESSAGE] + "..."
        if lines and spent + len(content) > MAX_HISTORY_CHARS:
            break
        spent += len(content)
        lines.append({
            "role": "user" if row.sender == "human" else "assistant",
            "content": content,
        })

    lines.reverse()
    return json.dumps(lines, ensure_ascii=False)


def fold_the_answer_in(env, session, answer: str) -> bool:
    """Put what they just answered inside the question that asked it.

    Returns False when there was nothing to fold into, and the caller then
    stores the answer on its own -- a duplicated answer is cosmetic, a lost one
    is the complaint this exists to prevent.
    """
    said = (answer or "").strip()
    if not session or not said:
        return False
    try:
        last = env["razyyn.agent.chat.message"].sudo().search(
            [("session_id", "=", session.id), ("sender", "=", "ai")], order="id desc", limit=1
        )
        if not last:
            return False
        content = last.content or ""
        found = transcript._QUESTION_PAYLOAD.search(content)
        if not found:
            return False
        if transcript._ANSWERED.search(content):
            # Already settled: this reply belongs to a later question, and
            # writing it here would attribute it to one answered long ago.
            return False

        questions = json.loads(transcript.b64decode(found.group(1)).decode("utf-8"))
        if not isinstance(questions, list):
            return False

        last.content = transcript._collapsible_question(
            str(questions[0].get("question") or "") if questions else content,
            questions,
            answer=said,
        )
        env.cr.commit()
        return True
    except Exception as exc:
        _logger.warning("Razyyn AI: could not fold the answer in: %s", exc)
        return False


# ── Talking to the agent server ─────────────────────────────────────────────
def server_url(env) -> str:
    return configured_server_url(env)


class SessionEnded(Exception):
    """The platform refused our credentials and renewing them did not help."""


class PlatformUnreachable(Exception):
    """The platform could not be reached at all."""


def call_the_platform(env, settings, send):
    """Make one request as this customer, renewing the session once if it is stale.

    `send` is handed the headers and returns the response, so a request that
    has to be retried is built fresh -- an already-consumed upload cannot be
    sent twice.
    """
    token = (settings.sudo().access_token or "").strip()
    if not token:
        raise SessionEnded("Your session has ended. Please sign in again.")

    try:
        response = send({"Authorization": f"Bearer {token}"})
    except requests.exceptions.RequestException as exc:
        raise PlatformUnreachable(str(exc)) from exc

    if response.status_code != 401:
        return response

    refreshed = renew_access_token(env, settings)
    if not refreshed:
        raise SessionEnded("Your session has ended. Please sign in again.")
    try:
        return send({"Authorization": f"Bearer {refreshed}"})
    except requests.exceptions.RequestException as exc:
        raise PlatformUnreachable(str(exc)) from exc


def renew_access_token(env, settings) -> str:
    """Trade the refresh token for a new access token, and store the new pair.

    Returns "" when the session is genuinely over. Both tokens are written back
    together: the platform rotates the refresh token, and keeping the old one
    would end the session at the next renewal instead of this one.
    """
    refresh = (settings.sudo().refresh_token or "").strip()
    if not refresh:
        return ""
    try:
        response = requests.post(
            f"{server_url(env)}/auth/refresh",
            json={"refresh_token": refresh},
            timeout=(10, 20),
        )
    except requests.exceptions.RequestException as exc:
        _logger.warning("Razyyn AI: could not renew the session: %s", exc)
        return ""
    if response.status_code != 200:
        return ""
    try:
        body = response.json()
    except ValueError:
        return ""
    access = body.get("access_token") or ""
    if not access:
        return ""
    settings.sudo().write({
        "access_token": access,
        "refresh_token": body.get("refresh_token") or refresh,
    })
    env.cr.commit()
    return access


# ── The turn ────────────────────────────────────────────────────────────────
def start_turn(dbname: str, uid: int, session_id: str, message: str,
               agent_type: str, attachment_ids: list, scan: bool,
               high_thinking: bool = False) -> None:
    """Hand the turn to a worker thread and return immediately.

    Odoo has no job queue in core, so the worker is a thread with a cursor of
    its own. `request` does not exist inside it -- everything it needs is passed
    in as plain values, deliberately, so nothing can reach back into an HTTP
    request that has already been answered.
    """
    thread = threading.Thread(
        target=_run_turn,
        args=(dbname, uid, session_id, message, agent_type, list(attachment_ids or []), bool(scan), bool(high_thinking)),
        name=f"razyyn-turn-{session_id[:12]}",
        daemon=True,
    )
    thread.start()


def _run_turn(dbname, uid, session_id, message, agent_type, attachment_ids, scan, high_thinking=False):
    threading.current_thread().dbname = dbname
    registry = odoo.registry(dbname)
    with registry.cursor() as cr:
        env = api.Environment(cr, uid, {})
        session = _session_of(env, session_id, uid)
        if not session:
            _logger.warning("Razyyn AI: turn for an unknown session %s", session_id)
            return
        try:
            _drive_turn(env, uid, session, message, agent_type, attachment_ids, scan, high_thinking)
        except Exception as exc:  # pragma: no cover - the customer is told first
            _report_failure(env, uid, session, str(exc))
        finally:
            # However this ended -- answered, failed, cancelled, stopped by the
            # customer -- the turn sweeps up after itself. The ceiling is read
            # HERE, while this turn is still the last thing to have written, so
            # whatever a later turn writes is already out of its reach.
            schedule_event_cleanup(dbname, session_id, highest_event_id(env, session_id))


def _report_failure(env, uid, session, detail: str) -> None:
    """Tell the customer, then log. In that order, and nothing between them.

    On ERPNext this handler once wrote to the error log first, the log write
    itself failed, and the two lines that release the customer's page never ran
    -- so the chat span for ever with nothing recorded anywhere. Reporting comes
    first and logging last, each in its own guard.
    """
    try:
        save_chat_event_if_not_duplicate(env, session, "ai", f"{ERROR_PREFIX} {detail}")
    finally:
        publish(env, uid, session.session_id, "agent_message_error",
                {"session_id": session.session_id, "error": detail})
    _logger.error("Razyyn AI: the turn failed in session %s: %s", session.session_id, detail)


#: Picture formats the agent service decodes for itself. Anything else that is
#: a picture is converted before it travels, rather than refused on arrival.
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp",
                               ".bmp", ".tif", ".tiff"})
_PLATFORM_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def _reading_travels_as(display_name: str, already_used: set) -> str:
    """The name a reading is sent under: the document's own, ending in .txt.

    "invoice_04.png" becomes "invoice_04.txt" and not "invoice_04.png.txt",
    which reads as a mistake to everyone who sees it -- the customer, and a desk
    that quotes it back in a letter. Two documents in one message whose names
    differ only by their kind get numbered rather than merged.
    """
    stem = os.path.splitext(display_name)[0] or display_name
    name = f"{stem}.txt"
    count = 2
    while name.lower() in already_used:
        name = f"{stem} ({count}).txt"
        count += 1
    already_used.add(name.lower())
    return name


def _as_a_picture_the_service_can_read(data: bytes, display_name: str):
    """A PNG copy of a picture in a format the service does not decode, or None.

    None means "send the file as it is", which is the answer for everything
    that is not a picture and for the picture formats that already work.
    """
    extension = os.path.splitext(display_name.lower())[1]
    if extension not in _IMAGE_EXTENSIONS or extension in _PLATFORM_IMAGE_EXTENSIONS:
        return None
    try:
        from PIL import Image

        payload = io.BytesIO()
        with Image.open(io.BytesIO(data)) as picture:
            picture.convert("RGB").save(payload, format="PNG")
        payload.seek(0)
    except Exception as exc:
        _logger.warning("Razyyn AI: %s could not be turned into a PNG: %s",
                        display_name, exc)
        return None
    return (f"{display_name}.png", payload.getvalue(), "image/png")


def _attachments_as_parts(env, attachment_ids, scan: bool = False):
    """What actually travels for each attachment.

    `scan` is the customer's own choice, made with the "Scan & Extract Data"
    button beside the message box. IT, AND NOTHING ELSE, DECIDES.

        Reading a picture is not always what somebody wants. An accountant
        attaching a photograph of a whiteboard, a chart to look at, or a
        screenshot of an error wants the agent to SEE it -- turning it into a
        wall of half-recognised words is worse than useless. With the button
        off, every file travels exactly as it was sent, whatever we might have
        been able to read from it.

    A PICTURE OF AN INVOICE TRAVELS AS ITS WORDS, NOT AS A PICTURE. The text
    was read on this server moments ago by the same worker that is about to
    send it: faster, a fraction of the cost, and -- the part that matters in
    accounting -- a purpose-built reader gets a figure right far more often
    than a language model looking at a photograph. The picture never leaves
    the practice.

    A PICTURE WITH NO WORDS IN IT TRAVELS AS A PICTURE, because then it is not
    a document and its extracted text would be noise. Every upload arrives as
    one thing or the other; nothing is ever silently left behind.
    """
    parts = []
    names = []
    if not attachment_ids:
        return parts, names

    used_names = set()
    attachments = env["ir.attachment"].sudo().browse(list(attachment_ids)).exists()
    for attachment in attachments:
        display_name = attachment.name or f"upload-{attachment.id}"
        try:
            data = attachment.raw
        except Exception as exc:
            _logger.warning("Razyyn AI: could not read attachment %s: %s",
                            attachment.id, exc)
            continue
        if not data:
            continue

        extracted = ocr_service.reading_of(env, attachment) if scan else None
        if extracted:
            # THE PICTURE IT CAME FROM IS NAMED IN THE FIRST LINE, so a desk
            # reading the words can say which document a figure came off, and a
            # customer asking about "the invoice photo" is understood.
            body = f'Text read from the uploaded file "{display_name}".\n\n{extracted}'
            parts.append((
                "files",
                (_reading_travels_as(display_name, used_names),
                 body.encode("utf-8"), "text/plain"),
            ))
            names.append(display_name)
            continue

        converted = _as_a_picture_the_service_can_read(data, display_name)
        if converted:
            parts.append(("files", converted))
            names.append(display_name)
            continue

        parts.append((
            "files",
            (display_name, data, attachment.mimetype or "application/octet-stream"),
        ))
        names.append(display_name)

    return parts, names


def _drive_turn(env, uid, session, message, agent_type, attachment_ids, scan, high_thinking=False):
    settings = env["razyyn.agent.settings"].sudo().search([("user_id", "=", uid)], limit=1)
    if not settings or not settings.access_token:
        _report_failure(env, uid, session, "Not authenticated with Razyyn.")
        return

    payload = {
        "message": message,
        "history": build_history_payload(env, session),
        "custom_instructions": settings.custom_instructions or "",
        "session_id": session.get_backend_session_id(),
        "erp_system": "ODOO",
        "stream": "true",
        "selected_agent": agent_type or "auto",
        # The customer's High Thinking switch: on, the platform routes the
        # question to its consultant team. Off is the default and costs nothing.
        "high_thinking": "true" if high_thinking else "false",
    }

    if scan and attachment_ids:
        _read_the_attachments(env, uid, session, attachment_ids)
        if _cancel_was_requested(env, session):
            publish(env, uid, session.session_id, "agent_message_cancelled",
                    {"session_id": session.session_id})
            return

    files, _names = _attachments_as_parts(env, attachment_ids, scan=scan)

    # The last moment at which stopping is free. Past this the request is with
    # the platform, which cancels its own runs.
    if _cancel_was_requested(env, session):
        publish(env, uid, session.session_id, "agent_message_cancelled",
                {"session_id": session.session_id})
        return

    def send(headers):
        return requests.post(
            f"{server_url(env)}/agent/chat",
            data=payload,
            files=files or None,
            headers=headers,
            stream=True,
            timeout=STREAM_TIMEOUT,
        )

    try:
        response = call_the_platform(env, settings, send)
    except (SessionEnded, PlatformUnreachable) as stopped:
        _report_failure(env, uid, session, str(stopped))
        return

    if response.status_code == 499:
        save_chat_event_if_not_duplicate(env, session, "ai", CANCELLED_TEXT)
        publish(env, uid, session.session_id, "agent_message_cancelled",
                {"session_id": session.session_id})
        return

    if response.status_code != 200:
        try:
            detail = response.json().get("detail", "Error from the agent server.")
        except Exception:
            detail = response.text or "Error from the agent server."
        _report_failure(env, uid, session, str(detail))
        return

    _relay(env, uid, session, response)


def _cancel_was_requested(env, session) -> bool:
    """Read the stop flag from the database, not from this worker's memory.

    The customer presses stop in a different process from the one running the
    turn. Reading a cached copy of the session here is reading this worker's own
    idea of the flag, which was loaded before they pressed it.
    """
    env.cr.commit()
    session.sudo().invalidate_recordset(["cancel_requested"])
    return bool(session.sudo().cancel_requested)


def _relay(env, uid, session, response) -> None:
    """Turn the agent server's stream into the events the chat window draws.

    The mapping is the Frappe app's, event for event and key for key, because
    the browser code reading them is literally the same file.
    """
    session_id = session.session_id
    current_event = None
    answered = False
    last_error = ""

    held = HeldWords(lambda event, text: publish(
        env, uid, session_id, event, {"session_id": session_id, "chunk": text},
    ))

    # However this ends -- the answer finished, the stream broke, the worker
    # was stopped -- nothing stays held.
    try:
        for line in response.iter_lines(chunk_size=1):
            if not line:
                continue
            text = line.decode("utf-8").strip()
            if text.startswith("event:"):
                current_event = text[6:].strip()
                continue
            if not text.startswith("data:"):
                continue

            raw = text[5:].strip()
            try:
                data = json.loads(raw)
            except Exception:
                data = {"text": raw}

            # Some events arrive wrapped one level deeper. Unwrap, keeping any
            # outer keys the inner body did not already define.
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                inner = dict(data["data"])
                for key, value in data.items():
                    if key != "data" and key not in inner:
                        inner[key] = value
                data = inner

            if current_event == "text":
                held.hold("agent_message_chunk", data.get("text", ""))
                continue

            if current_event == "reasoning":
                # THE MODEL'S SCRATCHPAD IS NOT WRITTEN DOWN, AND THAT IS THE
                # POINT. On ERPNext this goes out over a socket and costs a
                # packet. Here every event is a row, and the thinking alone was
                # NINE ROWS IN TEN -- 37,000 of the 41,000 rows measured on a
                # live site in twenty minutes of use. It is the model reasoning
                # aloud about a customer's ledger, it is never part of the
                # answer, and nothing ever asks for it back.
                #
                # Dropped at the door rather than written and swept up
                # afterwards, because the cheapest row is the one that was
                # never inserted -- and because a customer's ledger is not
                # something to leave the model musing about in their own
                # database, however briefly.
                #
                # What the customer actually follows is untouched: the answer
                # itself, a step starting, reading the ledger, sending a
                # message, the checklist. All of those are below, and all of
                # them are still written.
                continue

            # Everything below is a milestone rather than prose, and must never
            # overtake the words it comes after.
            held.flush()

            if current_event == "node_start":
                # `label` is what the customer reads, written by the agent in
                # business language. A lookup table in the browser would have to be
                # kept in step with every pipeline rename, and when it fell behind
                # it would not fail -- it would caption every step "Processing...".
                publish(env, uid, session_id, "agent_node_start", {
                    "session_id": session_id,
                    "node": data.get("node", ""),
                    "label": data.get("label", ""),
                    "agent": data.get("agent", ""),
                })

            elif current_event == "todo":
                publish(env, uid, session_id, "agent_todo_update", {
                    "session_id": session_id,
                    "status": data.get("status", ""),
                    "tasks": data.get("tasks", []),
                })

            elif current_event == "tool_start":
                publish(env, uid, session_id, "agent_tool_start", {
                    "session_id": session_id,
                    "tool": data.get("tool", ""),
                    "label": data.get("label", ""),
                    "input": data.get("input", {}),
                    "agent": data.get("agent", ""),
                })

            elif current_event == "done":
                answered = True
                spoken, questions = transcript._readable_response(data.get("response", ""))
                if questions:
                    spoken = transcript._collapsible_question(spoken, questions)

                save_chat_history(env, session, "ai", spoken)
                publish(env, uid, session_id, "agent_message_done", {
                    "session_id": session_id,
                    "response": spoken,
                    "agent": data.get("agent", ""),
                })
                if questions:
                    # A pause that is a QUESTION opens the answer picker straight
                    # away rather than waiting for the renderer to notice it. The
                    # agent is waiting on a person.
                    publish(env, uid, session_id, "agent_clarification_requested",
                            {"session_id": session_id, "questions": questions})

            elif current_event == "aside":
                # A MANAGER SENTENCE THAT IS NOT THE TURN'S ANSWER — "added the
                # VAT check as step 4", a reply to something asked while the
                # work ran, the sentence that presents a waiting step's
                # question. Stored like any message and drawn as its own
                # bubble above the working one; the run goes on.
                spoken = data.get("text", "")
                if spoken:
                    save_chat_history(env, session, "ai", spoken)
                    publish(env, uid, session_id, "agent_aside",
                            {"session_id": session_id, "text": spoken})

            elif current_event == "noted":
                # THE MANAGER HEARD IT MID-RUN. The composer stays open while
                # the manager works, and a message sent then reaches the run
                # in progress rather than starting one of its own. There is
                # no answer of this turn's own to wait for, so it simply ends.
                answered = True
                publish(env, uid, session_id, "agent_message_noted",
                        {"session_id": session_id})

            elif current_event == "cancelled":
                # The customer stopped the work. That IS this turn's answer;
                # nothing failed and nothing is missing.
                answered = True
                save_chat_event_if_not_duplicate(env, session, "ai", CANCELLED_TEXT)
                publish(env, uid, session_id, "agent_message_cancelled",
                        {"session_id": session_id})

            elif current_event == "error":
                # A STEP THAT FAILED IS NOT A TURN THAT FAILED. The manager runs a
                # checklist: one specialist can fail while the rest of the list
                # still runs and still has a report to give. The reason is
                # remembered and becomes the answer only if nothing else does.
                last_error = (data.get("detail") or data.get("message")
                              or data.get("error") or "")
                if last_error:
                    _logger.warning("Razyyn AI: a step failed in %s: %s", session_id, last_error)

    finally:
        held.flush()

    if not answered:
        raise RuntimeError(last_error or "The request ended without an answer.")


def _read_the_attachments(env, uid, session, attachment_ids) -> None:
    """Read any photographed or scanned uploads before the turn goes out.

    Progress is reported to the chat window as it goes, because reading ten
    pages takes long enough that a silent pause reads as a hang.
    """
    ocr_service.read_uploads(
        env,
        attachment_ids,
        on_progress=lambda note: publish(
            env, uid, session.session_id, "agent_scan_progress",
            dict(note, session_id=session.session_id),
        ),
        should_stop=lambda: _cancel_was_requested(env, session),
    )
