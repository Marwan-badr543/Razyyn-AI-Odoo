# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Giving the shared OCR reader somewhere to work, on Odoo.

services/ocr.py is the Frappe app's reader, copied whole. It works in terms of
FILES: it reads `invoice.png`, writes the words beside it as `invoice.png.txt`,
and next time it is asked about the same file it answers from that instead of
reading the picture twice.

Odoo keeps uploads in ir.attachment, which may or may not be a file on disk --
a small one can live in a database column. So this module gives every
attachment a stable path in a working directory of its own, and the reader's
own conventions then work unchanged.

WHY THE WORKING COPY IS NOT DELETED AT THE END OF THE TURN
    Because the reading beside it is the cache. An accountant who attaches the
    same twelve-page scan to a follow-up question should not wait through it
    being read a second time, and a model should not be charged for it twice.
    The hourly cleanup removes anything nothing has touched for a day.
"""

from __future__ import annotations

import logging
import os
import shutil
import time

from odoo import tools

from . import ocr

_logger = logging.getLogger(__name__)

#: A working copy nothing has touched for this long is not coming back.
WORKING_COPY_RETENTION_SECONDS = 24 * 60 * 60


def _working_directory(env) -> str:
    """Where working copies live: inside the site's own filestore.

    Not /tmp, which is emptied under the product's feet, and not next to the
    module, which is read-only on a proper deployment.
    """
    root = os.path.join(tools.config.filestore(env.cr.dbname), "razyyn_ai_uploads")
    os.makedirs(root, exist_ok=True)
    return root


def working_copy(env, attachment) -> str | None:
    """A path on disk holding this attachment's bytes, or None if it has none.

    Named by the attachment's id and checksum, so re-uploading the same
    document reuses the reading already made of it, while an attachment whose
    content changed gets a new path rather than a stale answer.
    """
    try:
        data = attachment.raw
    except Exception as exc:
        _logger.warning("Razyyn AI: could not read attachment %s: %s", attachment.id, exc)
        return None
    if not data:
        return None

    extension = os.path.splitext(attachment.name or "")[1].lower()
    checksum = (attachment.checksum or "")[:16] or str(len(data))
    path = os.path.join(
        _working_directory(env), f"{attachment.id}-{checksum}{extension}"
    )
    if not os.path.exists(path):
        # Written beside itself and moved into place, so a reader that arrives
        # mid-write never sees half a document and records that as its words.
        temporary = path + ".part"
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    return path


def configure_languages(env) -> None:
    """Tell the reader which alphabets to expect, once, before any page is read."""
    ocr.OCR_LANGUAGES = env["ir.config_parameter"].sudo().get_param(
        "razyyn_ai.ocr_languages", "eng+ara"
    )


def read_uploads(env, attachment_ids, on_progress=None, should_stop=None) -> None:
    """Read this message's pictures and scans, and say so while it happens.

    IT SAYS NOTHING ABOUT WORK IT IS NOT DOING. A PDF that already carries its
    own text, a spreadsheet, a picture read for an earlier message -- none of
    them are announced, because announcing them told a customer their typed
    invoice was being scanned and then that it had no words in it.
    """
    configure_languages(env)
    attachments = env["ir.attachment"].sudo().browse(list(attachment_ids or [])).exists()

    waiting = []
    for attachment in attachments:
        path = working_copy(env, attachment)
        if not path:
            continue
        pages = ocr.pages_to_read(path)
        if pages:
            waiting.append((path, attachment.name or os.path.basename(path), pages))

    if not waiting:
        return

    def say(position, filename, page, pages):
        if on_progress:
            on_progress({
                "filename": filename,
                "file": position,
                "files": len(waiting),
                "page": page,
                "pages": max(pages, 1),
            })

    try:
        for position, (path, name, pages) in enumerate(waiting, start=1):
            # Between files, because a document is read in one go: a customer
            # who pressed stop waits out the page in front of them, never the
            # other nine.
            if should_stop and should_stop():
                return
            say(position, name, 0, pages)
            ocr.read_upload(
                path,
                on_progress=lambda _stored, page, total, at=position, shown=name: say(
                    at, shown, page, total
                ),
            )
    finally:
        # ALWAYS, so the chat never leaves a progress bar in front of a
        # customer for work that has stopped. A reading that failed still lets
        # the message go: the picture is simply sent as it is.
        if on_progress:
            on_progress({"finished": True})


def reading_of(env, attachment) -> str | None:
    """The words already read out of this attachment, or None. Reads nothing."""
    path = working_copy(env, attachment)
    if not path:
        return None
    return ocr.reading_of(path)


def prune_working_copies(env) -> int:
    """Remove working copies and readings nothing has touched for a day."""
    root = _working_directory(env)
    cutoff = time.time() - WORKING_COPY_RETENTION_SECONDS
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed += 1
        except OSError:
            continue
    if removed:
        _logger.info("Razyyn AI: removed %s stale upload working copies", removed)
    return removed
