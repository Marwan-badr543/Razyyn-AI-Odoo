# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Sending a message on the company's behalf, from this site's own credentials.

WHY THE SENDING HAPPENS HERE AND NOT ON THE AGENT SERVER
    The agent could fetch this configuration over the API and call Telegram
    itself. That would put every customer's bot token across the network and
    into another process's memory. Keeping the call here means the agent asks
    for an outcome ("send this to that destination") and is never handed the
    credential — the same trust boundary the write gateway already draws.

WHY A MESSAGE IS TREATED LIKE A LEDGER WRITE
    An email to a client's auditor cannot be unsent. So it gets what a write
    gets: an idempotency key, so a retried request does not send twice, and a
    log row carrying the provider's OWN message id, because "no exception was
    raised" is not evidence that anything was delivered. The agent may tell a
    customer a message went out only when there is an id backing it.

WHAT THE AGENT READS, AND WHY THE SHAPES ARE NOT NEGOTIABLE FROM HERE
    ``agent/tools/messaging.py`` is the single client for every ERP.
    ``get_messaging_config`` must answer ``{"channels": {<name>: {...}}}`` — a
    dict keyed by channel name, where the client's ``describe_channels`` reads
    ``enabled``, ``unavailable_reason``, ``sender`` and ``destinations[].label``.
    A send must answer with a truthy ``provider_message_id`` or the client
    treats it as not sent, which is the correct direction for it to fail in.

THE THREE CHANNELS, AND WHY EMAIL IS CALLED ``gmail`` ON THE WIRE
    The client's channel list is ``gmail``, ``telegram``, ``slack``. That key
    is what the agent reads and what its approval gate names, so it stays
    ``gmail`` whatever actually carries the mail — a second spelling would
    simply be a channel the agent never offers. Here the mail is carried
    either by a mailbox the practice signed the agent in to over SMTP (a free
    Gmail address with an App Password, Outlook, anything) or, when they have
    not configured one, by this Odoo's own outgoing mail server.

EVERY MESSAGE WAITS FOR THE CUSTOMER
    Whatever the channel, the agent shows the recipient, the subject, the
    words and every file that would be attached, and sends only once the
    customer has approved it. That gate lives on the agent side — see
    ``agent/tools/messaging.py`` — and this module records who approved each
    message alongside who requested it.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
from email.utils import make_msgid
from urllib.parse import parse_qs, urlparse

import requests
from markupsafe import escape

from odoo import fields as odoo_fields

logger = logging.getLogger(__name__)

SETTINGS_MODEL = "razyyn.agent.messaging.settings"
LOG_MODEL = "razyyn.agent.message.log"

CHANNELS = ("gmail", "telegram", "slack")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
SLACK_API = "https://slack.com/api/{method}"

#: Total bytes of attachments in one message. Gmail's own hard limit is 25 MB
#: for the whole encoded message and Telegram's is 50 MB per document; 20 MB
#: stays under both with room for the third that base64 adds.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

HTTP_TIMEOUT = 30
BODY_PREVIEW_CHARS = 500

#: Telegram rejects the WHOLE request when a caption is longer than this, so a
#: long note is sent as its own message first rather than lost with the file.
TELEGRAM_CAPTION_LIMIT = 1024


# ─── Domain exceptions ───────────────────────────────────────────────────────


class MessagingError(Exception):
    """Base for everything this module refuses or fails to do."""

    def __init__(self, detail: str, code: str = "MESSAGING_ERROR") -> None:
        self.detail = detail
        self.code = code
        super().__init__(detail)


class ChannelNotConfiguredError(MessagingError):
    """The channel is off, or its settings are incomplete.

    Separate from a send failure because the remedy belongs to a different
    person: an administrator has to finish the setup, and telling the
    accountant "sending failed" sends them to the wrong place.
    """

    def __init__(self, channel: str, detail: str) -> None:
        super().__init__(detail, code=f"{channel.upper()}_NOT_CONFIGURED")


class UnknownDestinationError(MessagingError):
    """The named Telegram or Slack destination is not one the customer listed.

    A bot cannot start a conversation or post where it was never added, so the
    agent may not invent a chat id: an unlisted destination is not a delivery
    failure, it is a destination that does not exist.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail, code="UNKNOWN_DESTINATION")


class AttachmentError(MessagingError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, code="ATTACHMENT_REJECTED")


class ProviderRefusedError(MessagingError):
    """Telegram, Slack, or the mail server rejected the message."""

    def __init__(self, detail: str, code: str = "PROVIDER_REFUSED") -> None:
        super().__init__(detail, code=code)


# ─── Configuration ───────────────────────────────────────────────────────────


def _settings(env):
    """The site's messaging configuration, read with the tokens visible.

    ``sudo`` is load-bearing rather than habitual: the bot tokens carry
    ``groups="base.group_system"``, and Odoo answers a field the caller may not
    read with ``False`` instead of raising. Read without it, a perfectly
    configured site reports itself as having no token.
    """
    return env[SETTINGS_MODEL].sudo().get_settings_singleton()


def _destinations(settings, channel: str, with_address: bool = False) -> list[dict]:
    """What the agent is told about one channel's destinations.

    NAMES ONLY FOR THE BOT CHANNELS, and that is a guardrail rather than a
    habit: a chat id the agent never sees is a chat id it cannot invent, so an
    unlisted Telegram destination is refused instead of attempted.

    EMAIL IS THE EXCEPTION, because on email the omission protects nothing and
    costs something. Email reaches any address the agent types, so withholding
    a saved one buys no safety at all — while including it lets the customer be
    shown "Marwan (marwan@example.com)" on the approval card instead of a name
    they have to take on trust. A recipient nobody can check is not a recipient
    anybody can meaningfully approve.
    """
    rows = []
    for row in settings.destinations_for(channel):
        entry = {
            "label": row.label,
            "is_default": bool(row.is_default),
            "notes": row.notes or "",
        }
        if with_address:
            entry["address"] = row.address
        rows.append(entry)
    return rows


def get_messaging_config(env) -> dict:
    """Which channels are usable, and where the bot channels may send.

    Returns no secrets. The agent needs to know what it CAN do so it can answer
    honestly — "I can email that, but Telegram is not set up here" — and it
    needs the destination names so it can offer them. It never needs a token,
    so it is never sent one.
    """
    settings = _settings(env)

    email_ready = _email_ready(env, settings)
    # The saved recipients are an address book, not a fence: email is usable
    # with none of them, and an address the accountant gives is always sent to.
    # They are reported so the agent knows the names exist -- "email it to
    # Marwan" can only work if something has told it who Marwan is.
    email_destinations = _destinations(settings, "gmail", with_address=True)

    telegram_destinations = _destinations(settings, "telegram")
    telegram_ready = bool(
        settings.telegram_enabled
        and settings.telegram_bot_token
        and telegram_destinations
    )

    slack_destinations = _destinations(settings, "slack")
    slack_ready = bool(
        settings.slack_enabled
        and settings.slack_bot_token
        and slack_destinations
    )

    return {
        "success": True,
        "channels": {
            "gmail": {
                "enabled": email_ready,
                "sender": _email_sender(env, settings) if email_ready else None,
                "accepts_any_address": True,
                "destinations": email_destinations,
                "unavailable_reason": None if email_ready else _email_gap(env, settings),
            },
            "telegram": {
                "enabled": telegram_ready,
                # Stated explicitly because it is the constraint that surprises
                # people: the agent must not offer to "send it to this number".
                "accepts_any_address": False,
                "destinations": telegram_destinations,
                "unavailable_reason": None if telegram_ready
                else _telegram_gap(settings, telegram_destinations),
            },
            "slack": {
                "enabled": slack_ready,
                "accepts_any_address": False,
                "destinations": slack_destinations,
                "unavailable_reason": None if slack_ready
                else _slack_gap(settings, slack_destinations),
            },
        },
    }


def _own_smtp(settings) -> bool:
    """Has the practice given the agent a mailbox of its own to sign in to?

    This is the path that lets a small practice on a free Gmail address send at
    all. Nothing about it needs an Odoo administrator to have configured an
    outgoing mail server, and on many sites nobody ever has.
    """
    return bool(settings.sudo().smtp_host and settings.sudo().smtp_password)


def _email_ready(env, settings) -> bool:
    """Whether this site can actually put an email on the wire.

    THE SWITCH IS NOT THE CAPABILITY, and the difference is not academic. The
    flag defaults to on, and every Odoo has a company e-mail address — on a
    fresh database that address is Odoo's own demo value. Read as readiness,
    those two made the agent announce "I can email that, from
    info@yourcompany.com, to any address" on a site with no outgoing mail
    server at all; the accountant accepted, and the send failed at the mail
    server. Offering something and then failing is worse than saying plainly
    that it is not set up, because only one of the two leaves the customer
    knowing what to do.

    So readiness means all three: switched on, an address to send from, and
    SOMETHING to send through — the mailbox configured here, or failing that
    this Odoo's own outgoing mail server.
    """
    return bool(
        settings.email_enabled
        and _email_sender(env, settings)
        and (_own_smtp(settings) or env["ir.mail_server"].sudo().search_count([]))
    )


def _email_sender(env, settings) -> str:
    """The address email will actually leave from.

    Falls back to this company's own e-mail, which is what Odoo itself sends
    from when nothing more specific is set.
    """
    return settings.email_from or env.company.email or ""


def _email_gap(env, settings) -> str:
    """Which of the three things is missing, named so it can be fixed."""
    if not settings.email_enabled:
        return "Email sending is switched off in Razyyn AI's Messaging Channels."
    if not _email_sender(env, settings):
        return ("No sending address is set. Fill 'Send Email From' in Razyyn "
                "AI's Messaging Channels.")
    if not _own_smtp(settings) and not env["ir.mail_server"].sudo().search_count([]):
        return ("This system has no mail server to send through. Either fill "
                "the Mail Server boxes in Razyyn AI's Messaging Channels — for "
                "Gmail that is smtp.gmail.com with a 16-character App Password "
                "— or add an outgoing mail server under Settings → Technical → "
                "Outgoing Mail Servers.")
    return "Email is not fully configured."


def _telegram_gap(settings, destinations) -> str:
    if not settings.telegram_enabled:
        return "Telegram sending is switched off in Razyyn AI's Messaging Channels."
    if not settings.telegram_bot_token:
        return "No Telegram bot token has been saved yet."
    if not destinations:
        return (
            "No Telegram destinations have been added. A bot can only send to a "
            "chat that already exists, so each one has to be listed."
        )
    return "Telegram is not fully configured."


def _slack_gap(settings, destinations) -> str:
    if not settings.slack_enabled:
        return "Slack sending is switched off in Razyyn AI's Messaging Channels."
    if not settings.slack_bot_token:
        return "No Slack bot token has been saved yet."
    if not destinations:
        return (
            "No Slack destinations have been added. A bot can only post into a "
            "channel it has been invited to, so each one has to be listed."
        )
    return "Slack is not fully configured."


# ─── Attachments ─────────────────────────────────────────────────────────────

#: The two shapes a file address takes on this site: the chat's own download
#: route (what ``save_generated_file`` hands the agent) and Odoo's.
_ATTACHMENT_ID = re.compile(r"^/web/content/(\d+)")


def _attachment_id(url: str) -> int:
    parsed = urlparse(str(url))
    from_query = parse_qs(parsed.query).get("attachment_id")
    if from_query and str(from_query[0]).isdigit():
        return int(from_query[0])
    match = _ATTACHMENT_ID.match(parsed.path)
    if match:
        return int(match.group(1))
    return 0


def _load_attachments(env, file_urls) -> list[dict]:
    """Read the named files out of this site's own attachments.

    Only a file this site already holds can be sent. The agent passes the file
    ADDRESS it was given when the file was stored, never a path on disk:
    accepting a path would turn this endpoint into an arbitrary file read on
    the customer's server, reachable by anything that can reach the agent.
    """
    attachments: list[dict] = []
    total = 0

    for url in file_urls or []:
        if not url or not str(url).startswith("/"):
            raise AttachmentError(
                f"'{url}' is not a file on this system. Only files this system "
                f"already holds can be attached."
            )

        record_id = _attachment_id(url)
        attachment = env["ir.attachment"].sudo().browse(record_id) if record_id else None
        if not attachment or not attachment.exists():
            raise AttachmentError(f"No file on this system has the address {url}.")

        try:
            content = attachment.raw
        except Exception as exc:  # noqa: BLE001
            raise AttachmentError(
                f"{attachment.name} could not be read: {exc}"
            ) from exc
        if content is None:
            raise AttachmentError(f"{attachment.name} is empty.")
        if isinstance(content, str):
            content = content.encode("utf-8")

        total += len(content)
        if total > MAX_ATTACHMENT_BYTES:
            raise AttachmentError(
                f"The attachments come to more than "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB in total, which is "
                f"more than email and Telegram accept."
            )

        filename = attachment.name or "attachment"
        attachments.append({
            "filename": filename,
            "content": content,
            "mimetype": attachment.mimetype
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream",
        })

    return attachments


# ─── Destinations ────────────────────────────────────────────────────────────


def _resolve_email_destination(settings, destination) -> tuple[str, str]:
    """Turn what the accountant said into an address to email, and its name.

    EMAIL IS NOT A PERMITTED SET, AND THAT IS THE WHOLE DIFFERENCE. A Telegram
    bot can only reach a chat somebody added it to, so an unlisted destination
    there does not exist. Email reaches anybody, so the saved list is a
    convenience: it lets "send it to Marwan" work without the accountant
    finding the address again, and it takes nothing away — an address given in
    full is sent to exactly as before.

    Returns ``(address, label)``. The label is empty for an address that is not
    a saved one, which is what the receipt then says.
    """
    rows = settings.destinations_for("gmail")
    wanted = str(destination or "").strip()

    if "@" in wanted:
        # An address, given in full. Named in the receipt by its saved name
        # when it happens to be one of these, because "Sent to Marwan" is what
        # the accountant asked for and what they will recognise.
        for row in rows:
            if str(row.address).strip().casefold() == wanted.casefold():
                return wanted, row.label
        return wanted, ""

    if not wanted:
        default = next((r for r in rows if r.is_default), None)
        if default is None and len(rows) == 1:
            default = rows[0]
        if default is not None:
            return default.address, default.label
        if rows:
            names = ", ".join(r.label for r in rows)
            raise UnknownDestinationError(
                "I was not told who to email. Give me an address, or the name "
                f"of one of the saved recipients: {names}."
            )
        raise UnknownDestinationError("I was not told which address to email.")

    for row in rows:
        if row.label.strip().casefold() == wanted.casefold():
            return row.address, row.label

    if rows:
        names = ", ".join(r.label for r in rows)
        raise UnknownDestinationError(
            f"'{destination}' is not an email address, and nobody of that name "
            f"is saved here. The saved recipients are: {names}. Giving me the "
            f"address itself works too."
        )
    raise UnknownDestinationError(
        f"'{destination}' is not an email address. Give me the address to "
        f"send to, or save it under a name in Razyyn AI's Messaging Channels "
        f"so it can be asked for by name."
    )


def _resolve_destination(settings, channel: str, destination) -> tuple[str, str]:
    """Turn a name the accountant used into an address the customer listed.

    Returns ``(address, label)``. A raw id the agent made up is not accepted:
    the listed destinations are the whole permitted set, so a request naming
    anything else is refused rather than attempted.
    """
    rows = settings.destinations_for(channel)
    gap = _telegram_gap if channel == "telegram" else _slack_gap
    if not rows:
        raise ChannelNotConfiguredError(channel, gap(settings, []))

    if not destination:
        default = next((r for r in rows if r.is_default), None)
        if default is None and len(rows) == 1:
            default = rows[0]
        if default is None:
            names = ", ".join(r.label for r in rows)
            raise UnknownDestinationError(
                f"There is more than one {channel.title()} destination and none "
                f"is marked as the default, so I need to be told which one: {names}."
            )
        return default.address, default.label

    wanted = str(destination).strip().casefold()
    for row in rows:
        if row.label.strip().casefold() == wanted or str(row.address) == str(destination):
            return row.address, row.label

    names = ", ".join(r.label for r in rows)
    if channel == "telegram":
        raise UnknownDestinationError(
            f"'{destination}' is not one of the Telegram destinations set up "
            f"here. A Telegram bot cannot start a conversation, so it can only "
            f"send to a chat that has already been added: {names}."
        )
    raise UnknownDestinationError(
        f"'{destination}' is not one of the Slack destinations set up here. A "
        f"Slack bot can only post into a channel it has been invited to, so it "
        f"can only send to one that has already been added: {names}."
    )


# ─── Telegram ────────────────────────────────────────────────────────────────


def _send_telegram(settings, chat_id: str, body: str, attachments: list[dict]) -> str:
    """Send one Telegram message. Returns Telegram's own message id."""
    token = settings.telegram_bot_token
    if not token:
        raise ChannelNotConfiguredError("telegram", _telegram_gap(settings, []))

    if not attachments:
        response = requests.post(
            TELEGRAM_API.format(token=token, method="sendMessage"),
            json={"chat_id": chat_id, "text": body or "",
                  "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
        return _telegram_receipt(response)

    # With attachments the text rides as the caption of the first document, so
    # a one-file message arrives as one notification rather than two.
    message_id = ""
    for index, attachment in enumerate(attachments):
        payload: dict = {"chat_id": chat_id}
        if index == 0 and body:
            if len(body) <= TELEGRAM_CAPTION_LIMIT:
                payload["caption"] = body
            else:
                message_id = _telegram_receipt(requests.post(
                    TELEGRAM_API.format(token=token, method="sendMessage"),
                    json={"chat_id": chat_id, "text": body,
                          "disable_web_page_preview": True},
                    timeout=HTTP_TIMEOUT,
                ))

        response = requests.post(
            TELEGRAM_API.format(token=token, method="sendDocument"),
            data=payload,
            files={"document": (attachment["filename"], attachment["content"],
                                attachment["mimetype"])},
            timeout=HTTP_TIMEOUT,
        )
        message_id = _telegram_receipt(response) or message_id

    return message_id


def _telegram_receipt(response) -> str:
    if response.status_code >= 400:
        raise ProviderRefusedError(
            f"Telegram refused the message ({response.status_code}): "
            f"{_short_provider_error(response)}",
            code="TELEGRAM_REJECTED",
        )
    payload = _parsed(response)
    if not payload.get("ok"):
        raise ProviderRefusedError(
            f"Telegram refused the message: "
            f"{payload.get('description') or 'no reason given'}",
            code="TELEGRAM_REJECTED",
        )
    message_id = str((payload.get("result") or {}).get("message_id") or "")
    if not message_id:
        raise ProviderRefusedError(
            "Telegram accepted the request but returned no message id, so the "
            "send cannot be confirmed.",
            code="TELEGRAM_NO_RECEIPT",
        )
    return message_id


# ─── Slack ───────────────────────────────────────────────────────────────────


def _send_slack(settings, channel_id: str, body: str, attachments: list[dict]) -> str:
    """Send one Slack message. Returns Slack's own message id.

    Files go through Slack's external-upload flow — ask for an upload URL, put
    the bytes there, then complete the upload into the channel. The body rides
    as the first file's comment, the same shape ``_send_telegram`` gives.
    """
    token = settings.slack_bot_token
    if not token:
        raise ChannelNotConfiguredError("slack", _slack_gap(settings, []))

    headers = {"Authorization": f"Bearer {token}"}

    if not attachments:
        payload = _slack_payload(requests.post(
            SLACK_API.format(method="chat.postMessage"),
            headers=headers,
            json={"channel": channel_id, "text": body or "", "unfurl_links": False},
            timeout=HTTP_TIMEOUT,
        ))
        message_id = str(payload.get("ts") or "")
        if not message_id:
            raise ProviderRefusedError(
                "Slack accepted the request but returned no message id, so the "
                "send cannot be confirmed.",
                code="SLACK_NO_RECEIPT",
            )
        return message_id

    message_id = ""
    for index, attachment in enumerate(attachments):
        ticket = _slack_payload(requests.post(
            SLACK_API.format(method="files.getUploadURLExternal"),
            headers=headers,
            # This endpoint takes form fields, not JSON — JSON comes back as
            # `invalid_arguments` with no hint that the encoding was the problem.
            data={"filename": attachment["filename"],
                  "length": len(attachment["content"])},
            timeout=HTTP_TIMEOUT,
        ))

        put = requests.post(
            ticket["upload_url"],
            files={"file": (attachment["filename"], attachment["content"],
                            attachment["mimetype"])},
            timeout=HTTP_TIMEOUT,
        )
        if put.status_code >= 400:
            raise ProviderRefusedError(
                f"Slack did not accept the file upload ({put.status_code}): "
                f"{_short_provider_error(put)}",
                code="SLACK_REJECTED",
            )

        complete = {
            "files": [{"id": ticket["file_id"], "title": attachment["filename"]}],
            "channel_id": channel_id,
        }
        if index == 0 and body:
            complete["initial_comment"] = body
        _slack_payload(requests.post(
            SLACK_API.format(method="files.completeUploadExternal"),
            headers=headers, json=complete, timeout=HTTP_TIMEOUT,
        ))
        message_id = str(ticket["file_id"])

    return message_id


def _slack_payload(response) -> dict:
    """The parsed body of a Slack reply, or a refusal that names the cause."""
    if response.status_code >= 400:
        raise ProviderRefusedError(
            f"Slack refused the message ({response.status_code}): "
            f"{_short_provider_error(response)}",
            code="SLACK_REJECTED",
        )
    payload = _parsed(response)
    if not payload.get("ok"):
        error = str(payload.get("error") or "no reason given")
        if error == "not_in_channel":
            # The two refusals a customer will actually hit, so they name the fix.
            raise ProviderRefusedError(
                "Slack refused the message: the bot has not been invited to "
                "that channel. Open the channel in Slack and /invite the bot, "
                "then try again.",
                code="SLACK_NOT_IN_CHANNEL",
            )
        if error in ("missing_scope", "not_allowed_token_type"):
            # A token made for posting text alone cannot upload a file, and
            # Slack says so with the same two words whichever permission is
            # missing. Without naming both, an administrator reads
            # "missing_scope" and has nowhere to go.
            needed = str(payload.get("needed") or "") or "chat:write and files:write"
            raise ProviderRefusedError(
                f"Slack refused the message: the bot token does not carry the "
                f"permission this needs ({needed}). Add chat:write and "
                f"files:write to the app's Bot Token Scopes, REINSTALL the app "
                f"to the workspace — a scope added without reinstalling does "
                f"not take effect — and save the new token.",
                code="SLACK_MISSING_SCOPE",
            )
        raise ProviderRefusedError(
            f"Slack refused the message: {error}", code="SLACK_REJECTED",
        )
    return payload


# ─── Email ───────────────────────────────────────────────────────────────────


def _send_email(env, settings, to: str, subject: str, body: str,
                attachments: list[dict]) -> str:
    """Send one email, and return the id it will be reported by.

    THE PRACTICE'S OWN MAILBOX COMES FIRST. If they gave the agent a mail
    server to sign in to, that is what carries the message — a small practice on
    a free Gmail address has no Odoo outgoing mail server and never will, and
    telling them to go and configure one to send an invoice is telling them the
    product does not work for them. Only when they have not is this Odoo's own
    mail server used, which is what a company that already runs one expects.

    Either way an id comes back, and an id is the evidence: it means a server
    took the message. A mail queued and never sent is not reported as sent, so
    it goes immediately rather than waiting for the queue to pick it up.
    """
    if not to or "@" not in str(to):
        raise UnknownDestinationError(f"'{to}' is not an email address.")

    sender = _email_sender(env, settings)

    if _own_smtp(settings):
        return _send_email_over_smtp(env, settings, sender, to, subject, body,
                                     attachments)

    Attachment = env["ir.attachment"].sudo()
    attachment_ids = [
        Attachment.create({
            "name": item["filename"],
            "datas": base64.b64encode(item["content"]),
            "mimetype": item["mimetype"],
            "res_model": "mail.message",
            "res_id": 0,
        }).id
        for item in attachments
    ]

    mail = env["mail.mail"].sudo().create({
        "subject": subject or "(no subject)",
        "body_html": _as_html(body),
        "email_from": (f'"{settings.email_sender_name}" <{sender}>'
                       if settings.email_sender_name else sender),
        "email_to": str(to).strip(),
        "attachment_ids": [(6, 0, attachment_ids)] if attachment_ids else False,
        "auto_delete": False,
    })

    try:
        mail.send(raise_exception=True)
        mail.invalidate_recordset()
        if mail.state != "sent":
            raise ProviderRefusedError(
                f"The mail server did not send the message"
                f"{': ' + mail.failure_reason if mail.failure_reason else ''}.",
                code="EMAIL_NOT_SENT",
            )
        return str(mail.message_id or f"odoo-mail-{mail.id}")
    except ProviderRefusedError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderRefusedError(
            f"This system's mail server would not send the message: {exc}",
            code="EMAIL_REJECTED",
        ) from exc
    finally:
        # THE COPIES ARE THE SEND'S, NOT THE SITE'S. The file itself is already
        # stored here; these are second copies made only so the mail server had
        # something to attach, and they sit outside the hourly sweep that
        # clears generated reports. Left behind, every emailed report is kept
        # twice for ever.
        if attachment_ids:
            Attachment.browse(attachment_ids).unlink()


#: The port each security setting uses when the administrator named none.
#: Getting this wrong is the difference between "it works" and a connection
#: that hangs until it times out, which reads to a customer as a broken agent.
_SMTP_PORTS = {"starttls": 587, "ssl": 465, "none": 25}


def _send_email_over_smtp(env, settings, sender: str, to: str, subject: str,
                          body: str, attachments: list[dict]) -> str:
    """Send one email from the mailbox the practice configured here.

    THROUGH ODOO'S OWN SMTP MACHINERY, not a second implementation of it.
    ``ir.mail_server`` already knows how to open STARTTLS and SSL connections,
    how to fail helpfully when a certificate is wrong, and how to build a
    message Odoo's own logs can account for — and it takes the connection
    details as arguments, so none of that has to be written again here just
    because the credentials live on a different record.

    THE MESSAGE ID IS OURS, AND THAT IS THE WHOLE POINT. SMTP hands back
    nothing at all on success. The id is written into the header before the
    message leaves, so it appears both on the customer's receipt and in the
    recipient's own copy: given the reference, the message can be found.
    """
    settings = settings.sudo()
    security = settings.smtp_security or "starttls"
    port = int(settings.smtp_port or 0) or _SMTP_PORTS.get(security, 587)
    username = settings.smtp_username or sender
    encryption = {"starttls": "starttls", "ssl": "ssl", "none": "none"}[security]

    MailServer = env["ir.mail_server"].sudo()
    message_id = make_msgid(domain=(sender.rpartition("@")[2] or None))

    message = MailServer.build_email(
        email_from=(f'"{settings.email_sender_name}" <{sender}>'
                    if settings.email_sender_name else sender),
        email_to=[str(to).strip()],
        subject=subject or "(no subject)",
        body=_as_html(body),
        subtype="html",
        message_id=message_id,
        # THREE-TUPLES, NOT TWO. build_email's own docstring says "(filename,
        # filecontents) pairs" and its code unpacks (fname, fcontent, mime) —
        # a pair raises on the unpack and takes the whole send with it, which
        # would mean every message carrying a file failed and every message
        # without one worked.
        attachments=[
            (item["filename"], item["content"],
             item.get("mimetype") or "application/octet-stream")
            for item in attachments
        ],
    )

    connection = None
    try:
        connection = MailServer.connect(
            host=settings.smtp_host, port=port, user=username,
            password=settings.smtp_password, encryption=encryption,
        )
        MailServer.send_email(message, smtp_session=connection)
    except Exception as exc:  # noqa: BLE001
        reason = _smtp_reason(exc)
        if _looks_like_a_sign_in_refusal(reason):
            # THE ONE FAILURE EVERY GMAIL CUSTOMER HITS. Google refuses an
            # account password outright and says only "Username and Password
            # not accepted", which reads as a typo and sends them to retype the
            # very password that can never work.
            raise ProviderRefusedError(
                f"The mail server would not accept the sign-in for {username}. "
                f"If this is a Gmail address it needs a 16-character App "
                f"Password rather than the account's own password: turn on "
                f"2-Step Verification, create one at "
                f"myaccount.google.com/apppasswords, and save it in Razyyn "
                f"AI's Messaging Channels. The server said: {reason}",
                code="SMTP_AUTH_REFUSED",
            ) from exc
        raise ProviderRefusedError(
            f"The mail server {settings.smtp_host} would not send the message: "
            f"{reason}",
            code="SMTP_REJECTED",
        ) from exc
    finally:
        if connection is not None:
            try:
                connection.quit()
            except Exception:  # noqa: BLE001
                # The message has already left. A mail server that drops the
                # connection on the way out must not turn a delivered message
                # into a reported failure.
                logger.debug("The mail server did not close cleanly", exc_info=True)

    return message_id


def _looks_like_a_sign_in_refusal(reason: str) -> bool:
    """Was this the mail server rejecting the credentials?

    Read from the server's own words rather than from an exception type: Odoo
    wraps every SMTP failure in the same ``MailDeliveryException``, so the type
    says nothing about which of them happened, and the sign-in refusal is the
    one an administrator needs told apart from the rest.
    """
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in (
        "authentication", "auth failed", "username and password",
        "5.7.8", "535", "not accepted", "invalid credentials",
    ))


def _smtp_reason(exc: Exception) -> str:
    """The mail server's own words, decoded and trimmed.

    smtplib carries a server's reply as raw bytes, so an untouched exception
    reads as ``b'5.7.8 Username and Password not accepted'`` — quoting that at
    an accountant is quoting a Python repr at them.
    """
    detail = getattr(exc, "smtp_error", None)
    if isinstance(detail, (bytes, bytearray)):
        detail = detail.decode("utf-8", "replace")
    if not detail:
        # Odoo wraps the real cause; its own message is the readable half.
        detail = getattr(exc, "args", None) and exc.args[0] or exc
    return str(detail)[:300]


def _as_html(body: str) -> str:
    """The agent writes plain words; email carries HTML.

    Nothing is interpreted — the text is escaped and its line breaks kept — so
    a figure or a stray angle bracket in a report cannot become markup.
    """
    return "<pre style=\"font-family:inherit;white-space:pre-wrap\">" \
           f"{escape(body or '')}</pre>"


# ─── Shared helpers ──────────────────────────────────────────────────────────


def _parsed(response) -> dict:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _short_provider_error(response) -> str:
    """The provider's reason, trimmed, and never the whole body.

    A provider error body can carry the request back verbatim — which for a
    message with an attachment includes the file that was being sent.
    """
    payload = _parsed(response)
    if not payload:
        return (getattr(response, "text", "") or "")[:200]
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or "")[:300]
    if isinstance(error, str) and error:
        return error[:300]
    if payload.get("description"):
        return str(payload["description"])[:300]
    return str(payload)[:200]


def _body_with_subject(subject, body) -> str:
    """Telegram and Slack have no subject line, so a subject becomes line one.

    Dropping it instead would lose the one-line summary the agent wrote for the
    reader, which on a report is often the only thing naming what the file is.
    """
    subject = (subject or "").strip()
    body = (body or "").strip()
    if subject and body:
        return f"{subject}\n\n{body}"
    return subject or body


def _is_refusal(exc: MessagingError) -> bool:
    """Refused here, or failed at the provider.

    The distinction is what an operator reads first: a refusal is a
    configuration or request problem on this side, a failure is Telegram or the
    mail server saying no.
    """
    return isinstance(
        exc, (ChannelNotConfiguredError, UnknownDestinationError, AttachmentError),
    )


# ─── The public operation ────────────────────────────────────────────────────


def send_message(
    env,
    channel: str = "",
    destination: str = "",
    subject: str = "",
    body: str = "",
    file_urls=None,
    idempotency_key: str = "",
    run_id: str = "",
    session_id: str = "",
    approved_by: str = "",
    **_ignored,
) -> dict:
    """Send one message and return a receipt.

    Every outcome is written to the message log before this returns, refusals
    included — an outbound record holding only successes cannot answer "did the
    agent email that?", which is the question an auditor actually asks.

    A repeat of an idempotency key that already succeeded returns the ORIGINAL
    receipt and sends nothing. The agent retries on network failures, and a
    retry that emails a client's auditor a second copy of their trial balance
    is not a small mistake.

    ``**_ignored`` is not laziness: the controller hands this the request's own
    parameters, which carry the caller's API key alongside the message. Naming
    every field and swallowing the rest keeps the key out of this function
    without the route having to know what a message is made of.
    """
    channel = (channel or "").strip().lower()
    if channel not in CHANNELS:
        raise MessagingError(
            f"'{channel}' is not a channel this system can send through. "
            f"Available: {', '.join(CHANNELS)}.",
            code="UNKNOWN_CHANNEL",
        )
    if not idempotency_key:
        raise MessagingError(
            "An idempotency key is required.", code="MISSING_IDEMPOTENCY_KEY",
        )

    replay = _existing_receipt(env, idempotency_key)
    if replay is not None:
        return replay

    settings = _settings(env)
    label = ""
    resolved = str(destination or "")
    file_urls = list(file_urls or [])

    common = dict(
        env=env, idempotency_key=idempotency_key, channel=channel,
        subject=subject, body=body,
        # THE FILES' OWN NAMES, not the addresses they arrived as. A file
        # address on this site is a download route with the id in its query
        # ("…/download_file?attachment_id=7"), so a log built from the address
        # recorded every attachment in the system's history as "download_file".
        # Until the files are loaded there is nothing better than the address,
        # and an attempt refused at that point still owes the log a row.
        attachment_names=", ".join(str(url) for url in file_urls),
        approved_by=approved_by, run_id=run_id, session_id=session_id,
    )

    try:
        attachments = _load_attachments(env, file_urls)
        common["attachment_names"] = ", ".join(a["filename"] for a in attachments)

        if channel == "gmail":
            if not _email_ready(env, settings):
                raise ChannelNotConfiguredError("gmail", _email_gap(env, settings))
            resolved, label = _resolve_email_destination(settings, destination)
            provider_message_id = _send_email(
                env, settings, resolved, subject or "", body or "", attachments,
            )
        elif channel == "telegram":
            if not settings.telegram_enabled:
                raise ChannelNotConfiguredError(
                    "telegram", _telegram_gap(settings, []),
                )
            resolved, label = _resolve_destination(settings, "telegram", destination)
            provider_message_id = _send_telegram(
                settings, resolved, _body_with_subject(subject, body), attachments,
            )
        else:
            if not settings.slack_enabled:
                raise ChannelNotConfiguredError("slack", _slack_gap(settings, []))
            resolved, label = _resolve_destination(settings, "slack", destination)
            provider_message_id = _send_slack(
                settings, resolved, _body_with_subject(subject, body), attachments,
            )

    except MessagingError as exc:
        _record(status="refused" if _is_refusal(exc) else "failed",
                destination=resolved, destination_label=label,
                provider_message_id="", error_code=exc.code,
                error_message=exc.detail, **common)
        _remember_channel_error(settings, channel, exc.detail)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Sending a %s message failed", channel)
        _record(status="failed", destination=resolved, destination_label=label,
                provider_message_id="", error_code="UNEXPECTED",
                error_message=str(exc)[:500], **common)
        raise

    _record(status="sent", destination=resolved, destination_label=label,
            provider_message_id=provider_message_id, error_code="",
            error_message="", **common)
    _clear_channel_error(settings, channel)

    return {
        "success": True,
        "status": "SENT",
        "channel": channel,
        "destination": resolved,
        "destination_label": label,
        "provider_message_id": provider_message_id,
        "idempotency_key": idempotency_key,
    }


def _existing_receipt(env, idempotency_key: str):
    """The receipt for a key already sent, or None to go ahead.

    Only a SENT row short-circuits. A previous refusal or failure must be
    allowed to run again — the administrator may have fixed the very setting
    the first attempt complained about, and a retry is the natural next thing
    the accountant asks for.
    """
    row = env[LOG_MODEL].sudo().search([
        ("idempotency_key", "=", idempotency_key),
        ("status", "=", "sent"),
    ], limit=1)
    if not row:
        return None
    return {
        "success": True,
        "status": "SENT",
        "channel": row.channel,
        "destination": row.destination or "",
        "destination_label": row.destination_label or "",
        "provider_message_id": row.provider_message_id or "",
        "idempotency_key": idempotency_key,
        "replayed": True,
    }


def _record(*, env, idempotency_key, channel, status, destination,
            destination_label, subject, body, attachment_names, approved_by,
            run_id, session_id, provider_message_id, error_code,
            error_message) -> None:
    """Write the outbound record. Never raises.

    ON THIS REQUEST'S OWN CURSOR, AND THAT IS NOT A DETAIL. It was written on
    a second connection at first, so that a rollback of the request could not
    take the evidence of a delivered message with it. Odoo runs a request in
    REPEATABLE READ: a row another connection commits is invisible to the
    transaction already in flight. So the send that had just been recorded
    could not be found by the replay check a moment later, and the SAME message
    went out a second time — measured, twice delivered, against a live bot.
    Evidence nobody in this transaction can read is not evidence.

    AND IT IS NOT COMMITTED FROM HERE EITHER. Committing mid-request looks
    like the careful thing to do — the message has already left, so the record
    of it should not depend on the rest of the request. It is not: a commit
    destroys the savepoint the caller is running inside, and the caller is
    sometimes a test, sometimes a batch of several messages. The row is safe
    without it, because the route that reaches this catches every exception and
    answers with a response, and a request that returns a response commits.

    A logging failure must never turn a message that WAS sent into an exception
    the agent reports as a failure — the customer would be told nothing went
    out while their client already has it. The row is best-effort; the send is
    not.
    """
    values = {
        "idempotency_key": idempotency_key,
        "channel": channel,
        "status": status,
        "destination": (destination or "")[:250],
        "destination_label": (destination_label or "")[:250],
        "timestamp": odoo_fields.Datetime.now(),
        "subject": (subject or "")[:140],
        "body": (body or "")[:BODY_PREVIEW_CHARS],
        "attachment_names": (attachment_names or "")[:250],
        "requested_by": env.uid,
        "approved_by": (approved_by or "")[:140],
        "run_id": (run_id or "")[:140],
        "session_id": (session_id or "")[:140],
        "provider_message_id": (provider_message_id or "")[:140],
        "error_code": (error_code or "")[:64],
        "error_message": (error_message or "")[:500],
    }
    try:
        env[LOG_MODEL].sudo().create(values)
    except Exception:  # noqa: BLE001
        logger.exception("Could not write the agent message log row")


def _remember_channel_error(settings, channel: str, detail: str) -> None:
    """Surface the last failure on the settings form.

    The person who can fix a bad token is an administrator looking at that
    form, not the accountant reading the chat.
    """
    _set_channel_error(settings, channel, (detail or "")[:250])


def _clear_channel_error(settings, channel: str) -> None:
    _set_channel_error(settings, channel, "")


def _set_channel_error(settings, channel: str, value: str) -> None:
    field = f"{'email' if channel == 'gmail' else channel}_last_error"
    try:
        if settings.sudo()[field] != value:
            settings.sudo().write({field: value})
    except Exception:  # noqa: BLE001
        logger.exception("Could not record the last %s error", channel)
