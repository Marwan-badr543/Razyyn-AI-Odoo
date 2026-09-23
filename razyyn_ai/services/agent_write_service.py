# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Odoo's implementation of the Razyyn write-gateway protocol.

WHAT THIS FILE OWES THE AGENT
    The agent reaches every ERP through one client
    (``agent/agent_create/adapters/gateway.py``) and seven endpoints. That
    client is not negotiable from here: it is the same code that writes into
    ERPNext ledgers in production, and it reads particular keys out of
    particular replies. This file's whole job is to answer those seven calls in
    the shapes it reads.

    An earlier revision answered in shapes of its own — ``report`` where the
    client reads ``preflight``, ``issues`` where it reads ``findings``, a list
    of wrappers where it reads a list of records. Nothing raised. The agent
    simply found no accounts, had no validation findings and wrote nothing,
    which is the worst way for an integration to break, because it looks like
    an empty database rather than a bug.

THE PROTOCOL'S VOCABULARY IS NOT ODOO'S, AND THAT IS DELIBERATE
    On the wire a model is a ``doctype``, a record is a ``docname``, and where
    a document stands is a ``docstatus`` of 0/1/2. Those words came from the
    protocol's first implementation. Renaming a live wire format across three
    repositories buys nothing, so instead this file is the ONE place Odoo's own
    words are translated into them — ``account.move`` into a doctype,
    ``state='posted'`` into docstatus 1, ``many2one`` into ``Link``.

    The field-type translation matters more than it looks. The agent decides
    whether a field needs an existing record looked up by asking whether its
    type is ``Link``, and whether a value is money by asking whether it is
    ``Currency``. An Odoo ``many2one`` reported as "many2one" is a field the
    agent will never resolve a customer into; a ``monetary`` reported as
    "monetary" is an amount it will never recognise as an amount.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional, Sequence

from odoo import fields as odoo_fields
from odoo.exceptions import UserError, ValidationError

from .link_match import pick_link_match, spellings_of
from .write_guard import (
    AgentWriteError,
    MissingParameterError,
    ResourceNotFoundError,
    WriteRejectedError,
    check_write_policy,
)

logger = logging.getLogger(__name__)

#: The most documents one request may carry. Matches the agent's own
#: ``AdapterCapabilities.max_batch_size``, which is what it chunks a large
#: import at — a lower number here silently refuses whole chunks.
MAX_BATCH_SIZE = 50

#: Odoo field type -> the protocol's type name. Anything unlisted falls back to
#: ``Data``, which is the harmless direction: the agent treats it as free text
#: and asks, rather than assuming.
_FIELD_TYPES: dict[str, str] = {
    "char": "Data",
    "text": "Text",
    "html": "Text Editor",
    "integer": "Int",
    "float": "Float",
    "monetary": "Currency",
    "boolean": "Check",
    "date": "Date",
    "datetime": "Datetime",
    "selection": "Select",
    "many2one": "Link",
    "one2many": "Table",
    "many2many": "Table MultiSelect",
    "binary": "Attach",
    "json": "JSON",
}

#: Odoo's own state values in the protocol's 0/1/2 encoding.
#:
#: Odoo has no single ``docstatus`` column, and what a posted document's state
#: is called differs per model (``posted`` on a journal entry, ``sale`` on a
#: sales order, ``done`` on a delivery). The agent must learn none of those
#: words: it has four states, and this is where a model's own vocabulary
#: becomes one of them.
#:
#: KNOWN APPROXIMATION, stated rather than left looking authoritative. These
#: are the spellings Odoo's own accounting, sales and inventory models use, and
#: they are right for every model the agent writes to today. They are a guess
#: about VOCABULARY, though, not a fact: a module this connector has never seen
#: could use ``done`` to mean something that is not "final", and such a
#: document would read back as posted when it is not. The safe direction is the
#: one taken below — anything unrecognised falls to DRAFT, so an unknown state
#: is never reported to a customer as posted. Extend this map when a customer's
#: own module needs it; do not widen it by guessing.
_DRAFT, _POSTED, _CANCELLED = 0, 1, 2
_STATE_CODES: dict[str, int] = {
    "draft": _DRAFT,
    "posted": _POSTED,
    "done": _POSTED,
    "sale": _POSTED,
    "purchase": _POSTED,
    "paid": _POSTED,
    "cancel": _CANCELLED,
    "cancelled": _CANCELLED,
}

#: How a document gets posted, in the order Odoo's own buttons are tried.
_POST_ACTIONS = ("action_post", "button_post", "action_confirm", "action_validate")

#: How a document gets cancelled.
_CANCEL_ACTIONS = ("button_cancel", "action_cancel")

#: Bookkeeping columns that are Odoo's to write, never the agent's.
_SYSTEM_FIELDS = frozenset({
    "id", "__last_update", "display_name",
    "create_uid", "create_date", "write_uid", "write_date",
})

#: Model namespaces that are Odoo's plumbing rather than a customer's books.
#:
#: Every Odoo business model inherits `mail.thread` and `mail.activity.mixin`,
#: which bolt on a dozen relations — followers, activities, message threads,
#: attachments, website visitors. A journal entry therefore declares child
#: tables for `mail.activity` and `ir.attachment` alongside its actual lines.
#:
#: Listing them is not merely untidy. The spec is what the agent is SHOWN
#: before it composes a document, and a model handed `activity_ids` and
#: `message_ids` beside `line_ids` has three plausible places to put the rows
#: of an invoice. Cutting them is what makes `line_ids` the obvious one.
_FRAMEWORK_NAMESPACES = ("mail.", "ir.", "bus.", "utm.", "website.", "digest.")


def _is_framework(comodel: str) -> bool:
    return bool(comodel) and comodel.startswith(_FRAMEWORK_NAMESPACES)


def _is_writable(field) -> bool:
    """Whether a value the agent writes for this field would actually be kept.

    THE OLD RULES HID HALF OF A JOURNAL ENTRY, and the failure they caused is
    worth recording. They dropped every computed field without an inverse and
    every readonly field — reasonable-sounding, and wrong for modern Odoo,
    where the accounting documents are built almost entirely out of two
    patterns those rules cannot see:

      * ``compute=..., store=True, readonly=False`` — Odoo's own "derived
        default the user may override": a written value is stored and WINS
        over the compute. ``account.move.date`` and ``account.move.line.name``
        are exactly this.
      * a plain stored field with ``readonly=True`` — a UI hint only since
        Odoo 16; the ORM accepts the value at create. ``account.move.
        move_type`` (readonly, required, default "entry") is the field that
        decides what KIND of document a move is, and a spec without it cannot
        describe an invoice at all.

    Both were left out of the published spec, so the platform's own field
    check refused every correct journal entry ("account.move has no 'date'
    field"), the agent re-read the live schema, saw that the field plainly
    exists, sent the same correct payload again, and looped until the customer
    cancelled. The spec and the database must never disagree about a field
    that is really there.

    What still stays out is the one thing Odoo genuinely discards: a computed
    field that is neither stored-and-editable nor invertible — writing it does
    nothing, so offering it invites a value that vanishes silently.
    """
    if field.inverse:
        return True
    if field.compute:
        return bool(field.store and not field.readonly)
    return True

#: "update" AND "amend" ARE NOT THE SAME WORD TWICE. An amendment supersedes a
#: document that has already been POSTED — it is reversed and a corrected
#: successor takes its place. An update edits a DRAFT, which nothing has been
#: recorded from, so the record itself changes and there is nothing to
#: supersede. Collapsing them would let a request to fix a typo reverse a
#: posted entry.
_VALID_ACTIONS = ("create", "update", "submit", "cancel", "amend")


# ─── Translating between Odoo and the protocol ───────────────────────────────


def _docstatus(record) -> int:
    """Where *record* stands, in the protocol's encoding.

    A model with no ``state`` field is not "unknown" — it is a master record
    that is simply saved, which the protocol calls a draft. That is the same
    answer Frappe gives for a DocType that is not submittable.
    """
    state = getattr(record, "state", None)
    if not state:
        return _DRAFT
    return _STATE_CODES.get(str(state).lower(), _DRAFT)


def _is_submittable(model) -> bool:
    """Whether this model has a posting step at all.

    Asked of the model's own behaviour rather than of a list of model names: a
    customer's installed modules include ones this connector has never heard
    of, and a hard-coded list answers "no" for every one of them — which tells
    the agent the document needs no posting and leaves the entry in draft.
    """
    return any(hasattr(model, action) for action in _POST_ACTIONS)


def _field_spec(field_name: str, field) -> dict[str, Any]:
    """One field, in the shape the client's ``_field`` reads.

    ``reqd``, not ``required``: the client reads the Frappe spelling, and a
    correctly-populated ``required`` key it never looks at is how every
    mandatory field on Odoo came back optional.
    """
    options: Optional[str] = None
    if field.type in ("many2one", "one2many", "many2many"):
        options = field.comodel_name
    elif field.type == "selection":
        # Newline-separated, which is how the protocol carries a choice list
        # and what the agent's prompt renderer splits on.
        try:
            options = "\n".join(str(value) for value, _label in (field.selection or []))
        except (TypeError, ValueError):
            options = None

    return {
        "fieldname": field_name,
        "label": field.string or field_name,
        "fieldtype": _FIELD_TYPES.get(field.type, "Data"),
        # "Required" here means REQUIRED OF THE AGENT. A field Odoo fills in
        # by itself — a default, a compute, a related value — must not be
        # demanded of a payload: marking `auto_post` (required, default "no")
        # as reqd made the platform insist on a value Odoo was always going
        # to supply, on every single journal entry.
        "reqd": bool(field.required and field.default is None
                     and not field.compute and not field.related),
        # Read-only in the protocol means "the system derives this; do not
        # write it". Odoo's readonly flag has been a pure UI hint since v16,
        # so it maps to the protocol only on derived fields; a plain stored
        # field marked readonly (move_type) is genuinely the agent's to set.
        "read_only": bool(field.readonly and (field.compute or field.related)),
        # THE SYSTEM WORKS THIS ONE OUT FOR ITSELF.
        #
        # Odoo's commonest accounting pattern is `compute=..., store=True,
        # readonly=False`: the system derives a value and keeps yours if you
        # send one. `account.move.line.account_id`, `price_unit`, `tax_ids`,
        # `balance`; `account.move.journal_id`, `currency_id`, `date`,
        # `invoice_payment_term_id` are all of this kind — an invoice line
        # carrying nothing but a product and a quantity comes out complete,
        # priced and taxed, because the system fills the rest in from the
        # product, the partner and the journal.
        #
        # Published because a spec that does not say so reads as "twenty
        # ordinary fields you had better fill in". Live, that is exactly what
        # happened: asked for a two-line invoice the agent went looking for
        # the customer's receivable account, the product's selling price, the
        # product's tax, the category's income account and the company's
        # journal — seventy-three failed queries against internal tables that
        # hold none of those as plain columns, and a payload that then
        # duplicated every line the system was going to build anyway.
        #
        # It is NOT `read_only`: a written value is kept and wins, so a
        # journal entry's debit and credit — stated by the work — still go in.
        # The rule the agent is given is "send it when the work states it;
        # never go looking it up".
        "derived": bool(field.compute and field.store and not field.readonly),
        "options": options,
        "default": None,
    }


def _readable(exc: Exception) -> str:
    """An Odoo exception as one sentence an accountant could act on."""
    args = getattr(exc, "args", None)
    text = str(args[0] if args else exc).strip()
    return " ".join(text.split())[:400] or "This document was refused."


def _could_not_process(doctype: str) -> str:
    """What the customer reads when their system CRASHED on a document.

    Every other refusal this gateway gives is a sentence the system wrote for
    a person. An exception from outside the user-facing family is not one: its
    text is Python talking to a programmer, and handing it over tells the
    customer that the accounting reason their document was refused was an
    "unsupported operand type". So it names the document type and nothing
    else, while the class, the message and the traceback go where the operator
    will look for them.
    """
    named = (doctype or "").strip() or "document"
    return (
        f"Your system could not process this {named}. The details have been "
        f"saved to its log for your administrator."
    )


# ─── get_document_spec ───────────────────────────────────────────────────────


def resolved_doctype(env, doctype: str) -> str | None:
    """The technical model name for a document type, or None if there is none.

    EVERY ROUTE THAT TAKES A DOCUMENT TYPE ASKS THIS, AND THEY USED TO ASK IT
    FIVE DIFFERENT WAYS
        `doctype not in env` appeared in five places here, each answering a
        label with its own kind of wrong: the spec route said "not a model in
        this Odoo system", the search route said NOT_INSTALLED — which reads to
        the agent as "this customer does not have that module", and so to the
        customer as "your accounting system cannot record sales invoices" — the
        look-up route said the document did not exist, and preflight said the
        type did not. One question, four different false answers, all from the
        name Odoo itself prints on the menu.

        `resolve_model_name` is the one answer. It is the read side's, so the
        desk that reads a document and the desk that records one agree.
    """
    from . import agent_api_service

    try:
        return agent_api_service.resolve_model_name(env, doctype)
    except (agent_api_service.ResourceNotFoundError,
            agent_api_service.MissingParameterError):
        return None


def _doctype_hint(env, doctype: str) -> str:
    """What this system DOES have, for a refusal that can be acted on."""
    from . import agent_api_service

    return agent_api_service._model_suggestions(env, str(doctype or ""))


def _mark_tables_that_are_the_same_rows(child_tables: list[dict[str, Any]]) -> None:
    """Say which of a document's row tables are two views of ONE set of rows.

    TWO NAMES, ONE TABLE, AND FILLING IN BOTH DOUBLES THE DOCUMENT. An Odoo
    invoice publishes `invoice_line_ids` and `line_ids`; both are one2many onto
    `account.move.line` through the same `move_id`, and the first is simply the
    second filtered to the product lines. An agent reading the spec sees two
    plausible row tables and has no way to know that. Live, it filled in both
    with the same product line — and the entry came out at 750 against a stated
    330, with four lines where there were meant to be two.

    Detected rather than listed, from the two facts that define it: the same
    target model reached through the same linking field. Nothing here names a
    model or a field, so a document type nobody has looked at yet is covered
    the first time it is asked about.
    """
    by_rows: dict[tuple[str, str], list[str]] = {}
    for table in child_tables:
        key = (str(table.get("child_doctype") or ""), str(table.pop("_inverse", "")))
        if not all(key):
            continue
        by_rows.setdefault(key, []).append(str(table["fieldname"]))
    for table in child_tables:
        name = str(table["fieldname"])
        for names in by_rows.values():
            if name in names and len(names) > 1:
                table["same_rows_as"] = [other for other in names if other != name]


def build_document_spec(env, doctype: str) -> dict[str, Any]:
    """The field contract for one model, so the agent never guesses a name."""
    if not doctype:
        raise MissingParameterError("Document type is required.")

    # The SAME resolution the read side uses, deliberately.
    #
    # This used to refuse anything that was not already a technical name, so
    # "Journal Entry" — Odoo's own label for account.move, printed on the menu
    # the customer is looking at — was answered with "not a model in this Odoo
    # system". Two halves of one connector disagreeing about what a document
    # type is means the desk that READS a document and the desk that RECORDS
    # one have to be told the name twice, in different spellings.
    resolved = resolved_doctype(env, doctype)
    if resolved is None:
        raise ResourceNotFoundError(
            f"'{doctype}' is not a model in this Odoo system. "
            f"{_doctype_hint(env, doctype)}".strip()
        )
    doctype = resolved

    model = env[doctype]
    fields_spec: list[dict[str, Any]] = []
    child_tables: list[dict[str, Any]] = []

    for name, field in sorted(model._fields.items()):
        if name in _SYSTEM_FIELDS or not _is_writable(field):
            continue
        if name == "state":
            # The lifecycle column. The protocol carries lifecycle as ACTIONS
            # — submit posts, cancel reverses — and reads it back as
            # docstatus. A payload that wrote `state` directly would mark a
            # document posted without any of the posting logic running.
            continue
        if _is_framework(getattr(field, "comodel_name", "") or ""):
            continue

        if field.type == "one2many":
            comodel = field.comodel_name
            child_fields = []
            if comodel and comodel in env:
                for cname, cfield in sorted(env[comodel]._fields.items()):
                    if cname in _SYSTEM_FIELDS or cfield.type == "one2many":
                        continue
                    if not _is_writable(cfield):
                        continue
                    if _is_framework(getattr(cfield, "comodel_name", "") or ""):
                        continue
                    child_fields.append(_field_spec(cname, cfield))
            child_tables.append({
                "fieldname": name,
                "label": field.string or name,
                # `child_doctype`, not `child_type`: the client reads the
                # former. The latter arrived as None, so every line of every
                # journal entry was described to the model with no field list
                # at all — and the model then invented column names.
                "child_doctype": comodel,
                "fields": child_fields,
                # Filled in below, once every table of this document is known.
                "same_rows_as": [],
                "_inverse": getattr(field, "inverse_name", "") or "",
            })
            continue

        fields_spec.append(_field_spec(name, field))

    _mark_tables_that_are_the_same_rows(child_tables)

    return {
        # THIS SYSTEM'S OWN SPELLING, NOT THE CALLER'S. A caller's words are
        # not an identifier here; `_name` is what this model is actually
        # called, and it is what every later read must address.
        "doctype": model._name,
        "is_submittable": _is_submittable(model),
        "fields": fields_spec,
        "child_tables": child_tables,
    }


# ─── search_documents ────────────────────────────────────────────────────────


def _states_for(docstatus: Sequence[Any]) -> list[str]:
    """The Odoo state values matching a protocol docstatus filter."""
    wanted: list[str] = []
    for code in docstatus or ():
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            continue
        wanted.extend(name for name, value in _STATE_CODES.items() if value == code_int)
    return wanted


def search_documents(
    env,
    doctypes: Sequence[str],
    text: str = "",
    docstatus: Sequence[Any] = (),
    limit: int = 10,
    company: str = "",
) -> dict[str, Any]:
    """Existing records the agent may need to point at.

    Never raises for finding nothing. A model this database does not have is
    reported under ``unavailable`` with a reason, because "you do not have
    Sales installed" and "you have no sales orders" are different answers and
    the agent says different things about them.
    """
    documents: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    total = 0
    wanted_states = _states_for(docstatus)

    for raw in doctypes or ():
        doctype = str(raw).strip()
        if not doctype:
            continue
        resolved = resolved_doctype(env, doctype)
        if resolved is None:
            # Genuinely absent. NOT_INSTALLED is a real answer here and a very
            # wrong one for a document type that exists under its own label,
            # which is why it is only reached once resolution has failed.
            unavailable.append({
                "doctype": doctype,
                "reason": "NOT_INSTALLED",
                "hint": _doctype_hint(env, doctype),
            })
            continue
        doctype = resolved

        model = env[doctype].sudo()
        domain: list[Any] = []
        if company and "company_id" in model._fields:
            domain.append(("company_id.name", "ilike", company))
        if wanted_states and "state" in model._fields:
            domain.append(("state", "in", wanted_states))

        try:
            if text:
                # `name_search` is Odoo's own "find the record a person means".
                # It looks at whatever each model has decided identifies it —
                # an account's code, a partner's reference, an invoice's number
                # — not only a column literally called `name`. Searching
                # `name ilike` instead is why an account search by code found
                # nothing: on account.account the code lives in `code`, and
                # plenty of models have no `name` column at all.
                pairs = model.name_search(name=text, args=domain, limit=limit)
                records = model.browse([rid for rid, _label in pairs])
                matched = len(records)
            else:
                records = model.search(domain, limit=limit)
                matched = model.search_count(domain)
        except Exception as exc:  # noqa: BLE001 — one bad model must not sink the search
            logger.info("Could not search %s: %s", doctype, exc)
            unavailable.append({"doctype": doctype, "reason": "SEARCH_FAILED"})
            continue

        total += matched
        for record in records:
            company_name = _company_of(record)
            documents.append({
                "doctype": doctype,
                # The id as a string: `docname` is what the agent sends back to
                # act on this record, and Odoo identifies a record by its id.
                "docname": str(record.id),
                "label": record.display_name,
                "docstatus": _docstatus(record),
                "company": company_name,
                # WHO IT IS WITH. See `_record_party`: a draft's displayed name
                # names no party, so without this the agent cannot tell one
                # draft invoice from another on the card a person approves.
                "party": _record_party(record),
            })

    return {"documents": documents, "unavailable": unavailable, "total_matched": total}


def read_document_state(env, doctype: str, docname: str) -> dict[str, Any] | None:
    """ONE record, addressed by its own reference, or None if there is no such one.

    WHY THIS IS NOT A SEARCH. `search_documents` widens what it is given, so
    that somebody who half-remembered a name still finds their paperwork, and
    it answers with a page of the most recent matches. Both are right for
    offering a person a choice and wrong for looking a reference up: a caller
    that matches the reference exactly against a truncated page finds nothing
    and reports, with complete confidence, that the record does not exist.

    Answered in the same shape every other document route answers in, so the
    caller reads one thing whichever route found it.
    """
    if not doctype or not docname:
        raise MissingParameterError("Missing doctype or document name.")
    doctype = resolved_doctype(env, doctype)
    if doctype is None:
        return None

    record = _addressed(env, doctype, docname)
    if not record:
        return None

    return {
        "doctype": doctype,
        "docname": str(record.id),
        "label": record.display_name,
        "docstatus": _docstatus(record),
        "company": _company_of(record),
        "amount": _record_amount(record),
        # See `_record_party`. This is the line that would have shown the wrong
        # customer on the approval card before the invoice was posted.
        "party": _record_party(record),
    }


def _company_of(record) -> str:
    """Which company this record belongs to, by name, or nothing.

    Nothing is the honest answer for the models that have no company — a
    partner, a product — rather than this system's default, which would put a
    company name on a document that does not have one.
    """
    if "company_id" in record._fields and record.company_id:
        return record.company_id.name
    return ""


def _record_party(record) -> str:
    """Who a document is WITH, by name, or nothing.

    THE ONE FACT A PERSON CHECKS A DOCUMENT BY, AND IT WAS NOT PUBLISHED.
        A record's `display_name` is all the agent had to describe a document
        on the card a customer approves. For an unposted `account.move` that
        name is the literal string "Draft Invoice" — no party, no number,
        nothing that distinguishes it from every other draft on the database.
        So an invoice written to the wrong customer was approved, posted,
        reversed and re-entered across six approval rounds without the wrong
        name ever appearing on screen: each card truthfully said "Draft
        Invoice — 8000.0, YourCompany", about three different parties.

    Read from the record, never recalled, and empty for the models that have
    no counterparty — a company, a product — rather than inventing one.
    """
    for name in ("partner_id", "employee_id", "user_id"):
        field = record._fields.get(name)
        if field is None or field.type != "many2one":
            continue
        try:
            linked = record[name]
        except Exception:  # noqa: BLE001 — a label is never worth a failure
            continue
        if linked:
            return linked.display_name or ""
    return ""


def _record_amount(record) -> float | None:
    """The headline figure a person recognises a document by, or nothing."""
    for name in ("amount_total_signed", "amount_total", "amount", "price_total"):
        if name in record._fields:
            try:
                value = float(record[name] or 0.0)
            except (TypeError, ValueError):
                continue
            if value:
                return value
    return None


# ─── get_write_policy ────────────────────────────────────────────────────────


def load_write_policy(env) -> dict[str, Any]:
    """The customer's own rules about what the agent may record."""
    policy = env["razyyn.agent.write.policy"].sudo().get_policy_singleton()
    return {
        "enabled": bool(policy.enabled),
        "dry_run_only": bool(policy.dry_run_only),
        "require_approval": bool(policy.require_approval),
        # `allowed_document_types` is the key the client reads. An
        # `allowed_models` key it never looks at leaves the agent believing
        # every document type is permitted, and discovering otherwise only
        # when a write is refused mid-run.
        "allowed_document_types": (
            [m.model for m in policy.allowed_model_ids]
            if policy.restrict_to_listed_models else []
        ),
        "allowed_companies": [c.name for c in policy.allowed_company_ids],
        "max_documents_per_run": policy.max_documents_per_run or 0,
        "max_total_amount_per_run": policy.max_total_amount_per_run or 0.0,
    }


# ─── get_write_log / list_documents ──────────────────────────────────────────


def get_write_log(env, idempotency_key: str) -> dict[str, Any]:
    """What happened to one write, looked up by its idempotency key.

    The agent's only recovery path after a request whose answer never arrived.
    It has to tell "committed" apart from "we have no record of this", so a
    missing entry answers ``found: False`` rather than nothing at all.
    """
    if not idempotency_key:
        return {"found": False}

    log = env["razyyn.agent.write.log"].sudo().search(
        [("idempotency_key", "=", idempotency_key)], limit=1
    )
    if not log:
        return {"found": False}

    # THIS IS THE RECOVERY PATH, so the document has to be findable from it.
    # It answers the agent after a request whose reply never arrived, and the
    # receipt built from it is the only thing the customer will be shown about
    # a document that WAS written. An answer naming nothing but an internal id
    # leaves them with a document they cannot look up.
    identity = {}
    if log.target_model and log.target_record_id:
        identity = read_document_state(
            env, log.target_model, str(log.target_record_id),
        ) or {}

    return {
        "found": True,
        # `COMMITTED` is the one value the client acts on. Anything else means
        # "do not report this as written", which is right for a row still in
        # flight and for one that failed.
        "status": "COMMITTED" if log.status == "success" else str(log.status or "").upper(),
        "target_doctype": log.target_model or "",
        "target_docname": str(log.target_record_id or ""),
        "docstatus_written": log.docstatus_written,
        "action": log.action_type or "",
        "label": identity.get("label") or "",
        "company": identity.get("company") or "",
    }


def list_agent_documents(env, limit: int = 20, doctype: str = "") -> list[dict[str, Any]]:
    """Documents this agent recorded, newest first.

    Also how the agent reads a batch back: rather than one lookup per document
    it asks for its own recent writes and matches on (doctype, docname). Both
    keys therefore have to be spelled the protocol's way here.
    """
    domain = [("status", "=", "success")]
    if doctype:
        domain.append(("target_model", "=", doctype))

    logs = env["razyyn.agent.write.log"].sudo().search(
        domain, limit=max(1, int(limit)), order="timestamp desc, id desc"
    )
    return [
        {
            "doctype": log.target_model or "",
            "docname": str(log.target_record_id or ""),
            "docstatus": log.docstatus_written,
            "action": log.action_type or "",
            "session_id": log.session_id or "",
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
        }
        for log in logs
    ]


# ─── preflight ───────────────────────────────────────────────────────────────

_BLOCKING = "BLOCKING"


def _finding(field_path: str, message: str, severity: str = _BLOCKING) -> dict[str, str]:
    return {"field_path": field_path, "human_message": message, "severity": severity}


def _rows_of(raw: Any) -> list[dict[str, Any]]:
    """Child rows as plain dicts, whatever shape they arrived in.

    The agent sends a plain list of objects, because that is what a language
    model can reliably produce. Odoo's ORM wants ``(0, 0, values)`` commands.
    Both are read here, so a payload written either way works.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            rows.append(dict(item))
        elif isinstance(item, (list, tuple)) and len(item) >= 3 and isinstance(item[2], Mapping):
            rows.append(dict(item[2]))
    return rows


def _balance_findings(env, model, values: Mapping[str, Any]) -> list[dict[str, str]]:
    """Double-entry lines that do not balance.

    Found by SHAPE, never by model name. A hard-coded ``account.move`` check
    misses every localisation and custom module that also posts debits and
    credits, and it is exactly the rule that has to be rewritten the first time
    a customer installs something this connector has not heard of.
    """
    out: list[dict[str, str]] = []
    for field_name, field in model._fields.items():
        if field.type != "one2many" or field_name not in values:
            continue
        comodel = field.comodel_name
        if not comodel or comodel not in env:
            continue
        child_fields = env[comodel]._fields
        if "debit" not in child_fields or "credit" not in child_fields:
            continue

        rows = _rows_of(values.get(field_name))
        if not rows:
            continue
        debit = sum(float(r.get("debit") or 0.0) for r in rows)
        credit = sum(float(r.get("credit") or 0.0) for r in rows)
        if round(debit, 2) != round(credit, 2):
            out.append(_finding(
                field_name,
                f"The entry does not balance: debits total {debit:.2f} and "
                f"credits total {credit:.2f}.",
            ))
    return out


def preflight_document(env, payload: Mapping[str, Any], run_dry_run: bool = True) -> dict[str, Any]:
    """Check a payload without writing it, and answer in findings.

    Findings rather than an exception, because a refusal the agent can read is
    one it can fix: a missing field it can ask the customer about, an
    unbalanced entry it can correct. An exception is just "no".
    """
    doctype = str(payload.get("doctype") or "")
    if not doctype:
        return {"findings": [_finding("doctype", "No document type was given.")]}
    resolved = resolved_doctype(env, doctype)
    if resolved is None:
        return {"findings": [_finding(
            "doctype",
            f"'{doctype}' is not a document type in this system. "
            f"{_doctype_hint(env, doctype)}".strip(),
        )]}
    doctype = resolved

    try:
        check_write_policy(env, doctype, dict(payload))
    except AgentWriteError as exc:
        return {"findings": [_finding("policy", exc.message)]}

    model = env[doctype].sudo()
    values = {k: v for k, v in payload.items() if k != "doctype"}
    findings: list[dict[str, str]] = []

    # WHAT ODOO WILL FILL IN BY ITSELF, asked of Odoo rather than guessed.
    #
    # `required` on its own is not "the agent must supply this". On
    # account.move, `date` and `currency_id` are both required AND filled by
    # Odoo — from a default and from the journal respectively — the moment the
    # record is created. Treating required as "must be present in the payload"
    # therefore refused every correct journal entry with two findings the agent
    # could only satisfy by inventing a posting date and a currency, which is
    # precisely the fabrication this check exists to prevent.
    #
    # `default_get` is Odoo's own answer to "what does a new one of these start
    # out with", and a computed field is filled during create, so neither is
    # the agent's to provide.
    try:
        supplied_by_odoo = set(model.default_get(list(model._fields)))
    except Exception:  # noqa: BLE001 — a model that cannot answer just gets no relief
        supplied_by_odoo = set()

    for name, field in model._fields.items():
        if not field.required or field.readonly or field.type in ("one2many", "many2many"):
            continue
        if name in values or name in supplied_by_odoo or field.compute or field.related:
            continue
        findings.append(_finding(
            name, f"'{field.string or name}' is required and has no value."
        ))

    findings.extend(_balance_findings(env, model, values))

    if run_dry_run and not findings:
        findings.extend(_dry_run_findings(env, doctype, values))

    return {"findings": findings}


def _dry_run_findings(env, doctype: str, values: Mapping[str, Any]) -> list[dict[str, str]]:
    """What Odoo itself says about this payload, WITHOUT writing anything.

    NOTHING IS INSERTED, AND THAT IS THE WHOLE POINT
        The obvious implementation — create the record inside a savepoint and
        roll it back — validates perfectly and quietly damages the customer's
        books. Odoo hands out document numbers from PostgreSQL sequences, and a
        PostgreSQL sequence does not roll back. Every check of a sales order or
        a payment would burn a number, so a customer whose agent proposed ten
        documents and wrote three would find seven gaps in their numbering. In
        an audited ledger a numbering gap is not untidiness; it is a question
        the auditor asks and the accountant cannot answer.

        ``new()`` builds the record in memory instead. Onchanges and field
        conversion run, the model's own ``@api.constrains`` rules run, and no
        INSERT is issued — the same position the Frappe app takes, where
        preflight runs the DocType's real ``validate()`` and never inserts.

    IT FAILS OPEN, DELIBERATELY
        Only Odoo's user-facing refusals — ``ValidationError`` and
        ``UserError``, which are what a model raises when it means "a person
        got this wrong" — become findings. Anything else is a constraint that
        could not cope with an unsaved record, which is a limit of this check
        rather than a defect in the payload. Turning one of those into a
        blocking finding would refuse a document that is perfectly correct, and
        the agent would keep re-composing a payload that was right the first
        time. The real write still enforces everything, so nothing unsafe
        passes because of this.
    """
    try:
        record = env[doctype].sudo().new(_odoo_values(env, doctype, values))
    except (ValidationError, UserError) as exc:
        return [_finding("payload", _readable(exc))]
    except WriteRejectedError as refusal:
        # A reference naming a record this system does not have, or naming two.
        # It belongs here and not at the write: it is the agent's to fix, and
        # this is where the agent is told what is wrong while nothing has been
        # recorded yet.
        return [_finding(getattr(refusal, "field_path", "payload"), refusal.message)]
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not build a trial %s for preflight: %s", doctype, exc)
        return []

    try:
        record._validate_fields(list(values))
    except (ValidationError, UserError) as exc:
        return [_finding("payload", _readable(exc))]
    except Exception as exc:  # noqa: BLE001
        logger.info("Preflight constraints could not run on %s: %s", doctype, exc)
    return []


# ─── write_batch ─────────────────────────────────────────────────────────────


def _the_company_of(env, doctype: str, values: Mapping[str, Any]):
    """Which company this document says it is in, as an id, or None.

    Read BEFORE any other reference is resolved, because it is what settles the
    ones that are otherwise a genuine tie. A business with two companies has
    two journals called "Customer Invoices", two receivable accounts and two of
    most things — correctly, because they are two sets of books.
    """
    if "company_id" not in env[doctype]._fields:
        return None
    stated = values.get("company_id")
    if stated in (None, "", False):
        return None
    try:
        resolved = _link_target(env, "res.company", "company_id", stated)
    except Exception:
        # The company itself could not be resolved. Whatever is wrong with it
        # will be reported when it is written; it must not take the rest of the
        # document down from here.
        return None
    return resolved if isinstance(resolved, int) and resolved else None


def _in_this_company(target, matches, company):
    """The candidates that belong to *company*, plus the ones shared by all.

    TWO SETS OF BOOKS ARE TWO DIFFERENT RECORDS WITH ONE NAME, and which of
    them is meant is not a question for the customer — the document itself has
    already answered it. An invoice being raised in EG Company posts to EG
    Company's "Customer Invoices" journal; there is no other reading.

    A record belonging to no company in particular is kept, because it is
    usable from every company and so is never the wrong one of a pair.

    This is a TIE-BREAK, never a filter. It is reached only once a name has
    matched more than one record, so nothing narrows a search that was already
    going to succeed — which is the failure the company-is-a-default rule
    exists to prevent.
    """
    owner = "company_id" if "company_id" in target._fields else (
        "company_ids" if "company_ids" in target._fields else "")
    if not owner:
        return matches
    kept = []
    for record_id, label in matches:
        owned = target.browse(record_id)[owner]
        ids = owned.ids if hasattr(owned, "ids") else ([owned.id] if owned else [])
        if not ids or company in ids:
            kept.append((record_id, label))
    return kept or matches


def _derived_here(parent, field_name: str) -> bool:
    """Whether the document's own model works this field out for itself."""
    field = getattr(parent, "_fields", {}).get(field_name) if parent is not None else None
    return bool(field is not None and field.compute and field.store
                and not field.readonly)


def _link_target(env, model_name: str, field_name: str, value: Any,
                 company=None, parent=None) -> int:
    """The id of the record a Link field names, found the way a person would.

    THE AGENT SENDS A NAME, AND IT IS RIGHT TO.
        On the protocol a reference is a ``Link`` whose ``options`` say which
        type it points at, and its value is the record as the customer would
        say it: "EG Company", "Miscellaneous Operations", "400016 Office Rent".
        That is what ERPNext stores natively, and it is what an accountant
        writes down.

        Odoo stores an integer. Nothing translated between the two, so the
        journal entry above went to the database with ``company_id`` set to the
        text "EG Company" and PostgreSQL refused it:
        `invalid input syntax for type integer`. Every create the agent has ever
        attempted on Odoo failed on this line, with an error written for a
        database administrator rather than for the accountant reading it.

    FOUND BY ``name_search``, WHICH IS THE VENDOR'S OWN ANSWER to "which record
    does this text mean". It is what the Odoo user interface uses, so an account
    is matched on its code as well as its name, a partner on its reference, and
    a customer's own custom search on whatever they configured.

    AMBIGUITY IS REFUSED, NOT GUESSED. Two journals called "Bank" are two
    different sets of books, and quietly taking the first is how an entry lands
    in the wrong one and is found at year end.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return False

    target = env[model_name].sudo()

    # Tried as written first, and only then on the parts a displayed name is
    # built from. See link_match.spellings_of: this module hands the agent
    # "MISC/2026/09/0004 (RAZYYN-TEST-B)" as a document's name and Odoo's own
    # name_search does not match that string back.
    matches = []
    for spelling in spellings_of(text):
        matches = pick_link_match(
            spelling, target.name_search(name=spelling, operator="ilike", limit=8),
        )
        if matches:
            break

    if isinstance(matches, int):
        return matches

    if not matches and text.isdigit():
        # A NUMBER IS A NAME FIRST AND AN ID SECOND, and it took a wrong
        # posting to settle which way round.
        #
        # This used to read `if text.isdigit(): return int(text)` before any
        # lookup at all. Most of the world numbers its chart of accounts, so
        # `account_id: "400050"` -- the code an accountant reads off their own
        # trial balance -- went to the database as primary key 400050. Here
        # that key did not exist and PostgreSQL refused it, which was the lucky
        # outcome: on a database where some unrelated account happened to hold
        # that id, the entry would have posted to it, balanced, and been found
        # at year end.
        #
        # So the name is tried first, always, and an id is only what a number
        # means when it names nothing. Checked for existence rather than
        # trusted, so a number that is neither is refused rather than written.
        candidate = target.browse(int(text))
        if candidate.exists():
            return candidate.id

    if not matches:
        refusal = WriteRejectedError(
            f'I could not find "{text}" in your {model_name} records, so I have '
            f'not recorded anything for {field_name}.',
            code="LINK_NOT_FOUND",
        )
        refusal.field_path = field_name
        raise refusal
    if len(matches) > 1 and company:
        # The document has already said which set of books it is in.
        matches = _in_this_company(target, matches, company)
    if len(matches) == 1:
        return matches[0][0]
    if len(matches) > 1:
        names = ", ".join(f'"{label}"' for _id, label in matches[:3])
        # A NAME THIS SYSTEM WAS NEVER ASKED FOR. The fields it derives are
        # exactly the ones whose names repeat: a business with two companies
        # has two taxes called "14%", two journals called "Customer Invoices"
        # and two receivable accounts. Live, one such field was filled in by
        # name, could not be told apart, and a two-line invoice was rebuilt
        # three times and never recorded — over a tax this system was always
        # going to apply by itself. The refusal says so, because the way out is
        # not to pick one: it is to stop sending the field.
        leave_it_out = (
            f' This system fills "{field_name}" in for itself from the party, '
            f'the item and the journal — so the fix is not to pick one of '
            f'these. Leave "{field_name}" out of the document altogether and '
            f'let it be derived.'
            if _derived_here(parent, field_name) else ""
        )
        refusal = WriteRejectedError(
            f'"{text}" matches more than one of your {model_name} records '
            f'({names}). Tell me which one and I will use it.{leave_it_out}',
            code="LINK_AMBIGUOUS",
        )
        refusal.field_path = field_name
        raise refusal
    return matches[0][0]


def _refuse_two_views_of_one_table(model, values: Mapping[str, Any]) -> None:
    """Refuse a payload that fills in one set of rows twice under two names.

    THE SPEC SAYS SO IN ADVANCE AND THIS IS THE BACKSTOP. An Odoo invoice
    reaches the agent with both `invoice_line_ids` and `line_ids`, which are
    the same rows through the same link; a payload carrying both appends each
    line twice, and the resulting document balances, so nothing downstream
    complains. A doubled ledger that passes every check is the worst outcome
    this connector can produce, and it is not one the agent can be trusted to
    avoid by reading a note — a live plan carried both tables with the same
    product line on each.

    Refused rather than merged. Which of the two the agent meant is not
    knowable here, and picking one would silently discard rows somebody
    approved. The message names both, so the next attempt sends one.
    """
    filled = [
        name for name, value in values.items()
        if isinstance(value, (list, tuple)) and value
        and getattr(model._fields.get(name), "type", "") == "one2many"
    ]
    for position, name in enumerate(filled):
        field = model._fields[name]
        for other in filled[position + 1:]:
            twin = model._fields[other]
            if (field.comodel_name == twin.comodel_name
                    and getattr(field, "inverse_name", None)
                    and getattr(field, "inverse_name", None)
                    == getattr(twin, "inverse_name", None)):
                raise WriteRejectedError(
                    f"'{name}' and '{other}' are two views of the SAME rows on "
                    f"{model._name}, so filling in both records every line "
                    f"twice. Send the lines under one of them only.",
                    code="DUPLICATE_ROWS",
                )


def _odoo_values(env, doctype: str, values: Mapping[str, Any],
                 replacing: bool = False, company=None) -> dict[str, Any]:
    """The agent's payload as Odoo's ORM expects to receive it.

    Two translations, and both are Odoo's own conventions rather than anything
    the protocol should know about:

    * a list of row objects becomes a list of ``(0, 0, values)`` commands --
      without it Odoo refuses a journal entry whose lines are perfectly well
      formed;
    * a reference named the way a person names it becomes the integer id Odoo
      stores. See ``_link_target``.

    Rows are converted through this same function, so a reference inside a
    journal line is resolved exactly like one on the document itself.

    ``replacing`` IS THE DIFFERENCE BETWEEN CHANGING AN ENTRY AND DOUBLING IT.
        ``(0, 0, row)`` means "add this row". On a creation that is the only
        thing it can mean. On a CHANGE it is wrong, and wrong in the most
        expensive way an accounting agent can be: a customer asked for the two
        lines of a 300.00 draft to read 450.00, the payload carried both lines
        at 450.00, and Odoo appended them — leaving one entry with four lines
        and a value of 750.00 on each side. It balanced, so nothing complained,
        and the agent reported "now records a 450.00 expense" about a document
        that recorded 750.00.

        A table sent with a change is the whole of what that table should say
        afterwards: the payload has no row identities to match on, so there is
        no other reading available. ``(5, 0, 0)`` clears the table first, which
        is precisely what ``(6, 0, ids)`` — used for many-to-many below, and
        always has been — does for the other kind of collection.

        Nested rows are never ``replacing``: a row being created has nothing
        underneath it to replace.
    """
    model = env[doctype]
    _refuse_two_views_of_one_table(model, values)
    # SETTLED FIRST, BECAUSE EVERY OTHER REFERENCE MAY DEPEND ON IT. A row
    # inherits its document's company: an invoice line's account belongs to the
    # books the invoice is being raised in, and the line does not say so itself.
    company = _the_company_of(env, doctype, values) or company
    out: dict[str, Any] = {}
    for key, value in values.items():
        field = model._fields.get(key)
        if field is None:
            out[key] = value
        elif field.type == "one2many":
            rows = [
                (0, 0, _odoo_values(env, field.comodel_name, row, company=company))
                for row in _rows_of(value)
            ]
            out[key] = [(5, 0, 0), *rows] if replacing else rows
        elif field.type == "many2many":
            if isinstance(value, (list, tuple)):
                out[key] = [(6, 0, [
                    _link_target(env, field.comodel_name, key, item, company, model)
                    for item in value
                ])]
            else:
                out[key] = value
        elif field.type == "many2one":
            out[key] = _link_target(env, field.comodel_name, key, value, company, model)
        else:
            out[key] = value
    return out


def _payload_amount(payload: Mapping[str, Any]) -> float:
    """What this document is worth, for the policy's per-run ceiling."""
    for key in ("amount_total", "amount", "total", "grand_total"):
        if payload.get(key) is not None:
            try:
                return abs(float(payload[key]))
            except (TypeError, ValueError):
                continue
    for value in payload.values():
        rows = _rows_of(value)
        if rows:
            return sum(abs(float(r.get("debit") or 0.0)) for r in rows)
    return 0.0


def _record_log(env, *, idempotency_key, action, doctype, payload, session_id="",
                status="in_flight", record_id=0, docstatus_written=None,
                error_details="", rejection_reason="", dry_run=False):
    """Write or update this key's row in the audit log.

    Keyed by idempotency key, so a replay updates the same row rather than
    appending a second one — which is what lets ``get_write_log`` answer "what
    happened to this exact attempt" instead of "to something like it".
    """
    Log = env["razyyn.agent.write.log"].sudo()
    values = {
        "timestamp": odoo_fields.Datetime.now(),
        "user_id": env.uid,
        "session_id": session_id or "",
        "target_model": doctype or "",
        "target_record_id": record_id or 0,
        "action_type": action if action in _VALID_ACTIONS else "write",
        "status": status,
        "payload": json.dumps(payload, default=str) if isinstance(payload, (dict, list)) else str(payload or ""),
        "rejection_reason": rejection_reason or "",
        "idempotency_key": idempotency_key,
        "error_details": error_details or "",
        "dry_run": dry_run,
    }
    if docstatus_written is not None:
        values["docstatus_written"] = docstatus_written

    existing = Log.search([("idempotency_key", "=", idempotency_key)], limit=1)
    if existing:
        existing.write(values)
        return existing
    return Log.create(values)


def _result(entry, ordinal, outcome, *, docname="", docstatus=None,
            amount=None, label="", company="", error_code="",
            error_message="", party="") -> dict[str, Any]:
    """One row of the batch's answer, in the shape the client's receipt reads."""
    return {
        "ordinal": ordinal,
        # Echoed straight back. It is what lets the client attribute a refusal
        # to the right row of a four-hundred-row import; an answer without it
        # that reorders its results names the wrong document.
        "source_row_ordinal": entry.get("source_row_ordinal"),
        "idempotency_key": entry.get("idempotency_key") or "",
        "outcome": outcome,
        "doctype": entry.get("doctype") or "",
        "docname": docname or (entry.get("docname") or ""),
        "docstatus": docstatus,
        "amount": amount,
        # WHAT THE CUSTOMER WOULD CALL IT, AND WHERE IT IS. The receipt the
        # accountant reads is built from this row and from nothing else; a row
        # carrying only an internal id gives them nothing to look for.
        "label": label,
        "company": company,
        # WHO THE DOCUMENT IS WITH. A receipt naming only a draft's label says
        # "Draft Invoice" and nothing else; the party is what the accountant
        # reads it back by. See `_record_party`.
        "party": party,
        "error_code": error_code,
        "error_message": error_message,
    }


_REFERENCE_PREFIX = "@"


def _references(payload: Mapping[str, Any], docname: Any) -> set[int]:
    """Indexes of earlier documents in this batch that this one names."""
    found: set[int] = set()

    def scan(value: Any) -> None:
        if isinstance(value, str) and value.startswith(_REFERENCE_PREFIX) and value[1:].isdigit():
            found.add(int(value[1:]))
        elif isinstance(value, Mapping):
            for item in value.values():
                scan(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                scan(item)

    scan(dict(payload))
    scan(docname)
    return found


def _resolve_references(payload: Mapping[str, Any], produced: Mapping[int, str]) -> dict[str, Any]:
    """Replace every ``@N`` with the record item N actually produced."""

    def resolve(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(_REFERENCE_PREFIX) and value[1:].isdigit():
            target = produced.get(int(value[1:]))
            if target is None:
                raise WriteRejectedError(
                    f"This document refers to {value}, which was never recorded.",
                    code="DEPENDENCY_MISSING",
                )
            return int(target) if str(target).isdigit() else target
        if isinstance(value, Mapping):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [resolve(v) for v in value]
        return value

    return {k: resolve(v) for k, v in dict(payload).items()}


def _addressed(env, doctype: str, docname: Any):
    """ONE record, by this system's id or by the reference a person quotes.

    AN ID IS NOT THE ONLY ADDRESS A RECORD HAS. This system keys records by a
    number, and nobody outside it says "62" — they say "INV/2026/0001", which
    is what is printed on the document and what their accountant types. A
    look-up that understood only the number answered "there is no such
    document" to every reference a person could actually give, and the agent
    repeated it to them.

    ADDRESSED, NOT SEARCHED, and the difference is the `=`: an exact reference
    matching exactly one record. Two matches is not an address, so nothing is
    returned rather than one of them being picked — this answer decides which
    of somebody's documents gets posted or reversed.
    """
    model = env[doctype].sudo()
    text = str(docname or "").strip()
    if not text:
        return model.browse()
    if text.isdigit():
        record = model.browse(int(text))
        return record if record.exists() else model.browse()

    found = model.browse()
    if "name" in model._fields:
        found = model.search([("name", "=", text)], limit=2)
    if not found:
        try:
            pairs = model.name_search(name=text, operator="=", limit=2)
        except Exception as exc:  # noqa: BLE001 — a look-up is not a failure
            logger.info("Could not address %s %r: %s", doctype, text, exc)
            pairs = []
        found = model.browse([rid for rid, _label in pairs])
    return found if len(found) == 1 else model.browse()


def _existing(model, entry, doctype):
    """The record an action names, or a refusal saying which one was missing."""
    raw = entry.get("docname")
    record = _addressed(model.env, doctype, raw)
    if not record:
        raise ResourceNotFoundError(
            f"{doctype} {raw!r} is not one record in this system.")
    return record


def _invoke(record, actions, what: str) -> None:
    """Call whichever of *actions* this model actually has.

    A model with none of them cannot be posted or cancelled, and saying so is
    the honest answer. The alternative — which this replaced — was to try each
    in turn, find none, and return success for a document still sitting in
    draft. The agent would then tell the customer their entry was posted.
    """
    for name in actions:
        if hasattr(record, name):
            getattr(record, name)()
            return
    raise WriteRejectedError(
        f"{record._name} has no {what} step in this system, so it cannot be {what}ed.",
        code="NOT_SUBMITTABLE",
    )


def _apply(env, action, doctype, payload, entry, key, session_id, policy):
    """Do one document's worth of work, inside its own savepoint."""
    if action not in _VALID_ACTIONS:
        raise WriteRejectedError(
            f"'{action}' is not something I can do to a document.", code="INVALID_ACTION"
        )
    resolved = resolved_doctype(env, doctype) if doctype else None
    if resolved is None:
        raise ResourceNotFoundError(
            f"'{doctype}' is not a document type in this system. "
            f"{_doctype_hint(env, doctype)}".strip()
        )
    # Rebound, so the policy check, the write and the receipt all name the
    # model the database knows rather than the words the agent used.
    doctype = resolved

    check_write_policy(
        env, doctype, payload if action in ("create", "update", "amend") else None
    )

    if policy.dry_run_only:
        # `rejected`, NOT `success`. The row is the only record of this
        # idempotency key, and `get_write_log` decides whether a document
        # exists by reading `status` alone — it reports COMMITTED for
        # `success` and nothing else. A dry run logged as success therefore
        # tells the agent's timeout-recovery path that a document was written
        # when none was, and the customer is told an entry is in their ledger
        # that is not there. `dry_run=True` beside it is for a person reading
        # the log; it is not what the recovery path looks at.
        _record_log(env, idempotency_key=key, action=action, doctype=doctype,
                    payload=payload, session_id=session_id, status="rejected",
                    rejection_reason="Check-only mode: validated, not recorded.",
                    dry_run=True)
        raise WriteRejectedError(
            "This system is in check-only mode, so nothing was recorded.",
            code="DRY_RUN_ONLY",
        )

    _record_log(env, idempotency_key=key, action=action, doctype=doctype,
                payload=payload, session_id=session_id, status="in_flight")

    model = env[doctype].sudo()

    # One savepoint per document: all-or-nothing WITHIN a document, while a
    # failure leaves the documents already written in this batch untouched.
    with env.cr.savepoint():
        if action == "create":
            record = model.create(_odoo_values(env, doctype, payload))
        else:
            record = _existing(model, entry, doctype)
            if action == "update":
                _change_a_draft(env, record, doctype, payload)
            elif action == "amend":
                if payload:
                    record.write(
                        _odoo_values(env, doctype, payload, replacing=True))
            elif action == "submit":
                _invoke(record, _POST_ACTIONS, "post")
            elif action == "cancel":
                _invoke(record, _CANCEL_ACTIONS, "cancel")

    docstatus = _docstatus(record)
    _record_log(env, idempotency_key=key, action=action, doctype=doctype,
                payload=payload, session_id=session_id, status="success",
                record_id=record.id, docstatus_written=docstatus)
    # THE NAME AND THE PLACE, NOT ONLY THE ID. On Odoo a record's reference is
    # its database id, and a draft journal entry has no number of its own at
    # all — its `name` is literally "/" until it is posted. So a receipt built
    # from the id alone tells a customer "account.move 126 was recorded", and
    # they open Journal Entries and cannot see anything called that. Worse,
    # documents land in whichever company their accounts belong to, which need
    # not be the one their screen is showing: a customer looking at the wrong
    # company sees an empty list and concludes nothing was written. Both are
    # answered here, from the record itself, at the moment it was written.
    return (str(record.id), docstatus, _payload_amount(payload),
            record.display_name or "", _company_of(record),
            _record_party(record))


def _change_a_draft(env, record, doctype: str, payload: Mapping[str, Any]) -> None:
    """Change a DRAFT record's own values.

    THE DRAFT CHECK IS TAKEN HERE, INSIDE THE SAVEPOINT, and it is the whole
    safety of this action. A posted document is part of the customer's ledger:
    editing one rewrites a recorded figure with no reversal and no trail. The
    agent checks before it proposes the change and a person approves it — and
    then time passes, in which somebody may well have posted it. So the last
    word is taken at the moment of writing rather than trusted from the request.

    Written through ``write`` as the agent user's own rights allow, so this
    system's access rules and record rules apply exactly as they do to a
    creation.
    """
    if _docstatus(record) != _DRAFT:
        raise WriteRejectedError(
            f"{doctype} {record.id} is not a draft, so its contents cannot be "
            f"changed. A posted document is corrected by reversing it and "
            f"recording a corrected one in its place.",
            code="NOT_A_DRAFT",
        )
    if not payload:
        raise WriteRejectedError(
            f"There is nothing to change on {doctype} {record.id}.",
            code="NOTHING_TO_CHANGE",
        )
    record.write(_odoo_values(env, doctype, payload, replacing=True))


def _assert_run_caps(policy, documents: Sequence[Mapping[str, Any]]) -> None:
    """The customer's per-run ceilings, checked before anything is written.

    ONE check for the whole request. Per document it would let a request that
    breaches the ceiling write everything up to the document that crosses it —
    a half-applied import nobody asked for.
    """
    if policy.max_documents_per_run and len(documents) > policy.max_documents_per_run:
        raise WriteRejectedError(
            f"This would record {len(documents)} documents, and this system "
            f"allows at most {policy.max_documents_per_run} in one go.",
            code="RUN_CAP_DOCUMENTS",
        )
    if policy.max_total_amount_per_run:
        total = sum(_payload_amount(d.get("payload") or {}) for d in documents)
        if total > policy.max_total_amount_per_run:
            raise WriteRejectedError(
                f"This would record {total:,.2f} in one go, and this system "
                f"allows at most {policy.max_total_amount_per_run:,.2f}.",
                code="RUN_CAP_AMOUNT",
            )


def write_documents_batch(
    env,
    documents: Sequence[Mapping[str, Any]],
    run_id: str = "",
    session_id: str = "",
    approved_by: str = "",
) -> dict[str, Any]:
    """Every action of one request, in the order given.

    ATOMICITY, AND THE TWO HALVES ARE DIFFERENT QUESTIONS ON PURPOSE
        WITHIN a document — all or nothing, always. A half-written entry is a
        corrupt ledger, so each document runs in its own savepoint and a
        failure rolls that document back and nothing else.

        ACROSS documents — carry on, EXCEPT for documents that depended on one
        that failed. An import of four hundred invoices must not be discarded
        over row 397; an invoice for a customer that was never created must not
        be attempted at all, because Odoo's refusal would name a missing field
        rather than the real reason.

    BACK-REFERENCES ARE RESOLVED HERE. A field value of ``"@0"`` means "the
    record item 0 produced", and this loop is the only place that knows that id
    before the next item runs.
    """
    if not documents:
        raise MissingParameterError("No documents supplied.")
    if len(documents) > MAX_BATCH_SIZE:
        raise MissingParameterError(
            f"At most {MAX_BATCH_SIZE} documents may be written in one request."
        )

    policy = env["razyyn.agent.write.policy"].sudo().get_policy_singleton()
    if not policy.enabled:
        raise WriteRejectedError(
            "Agent writes are switched off in this system's Agent Write Policy.",
            code="POLICY_DISABLED",
        )
    _assert_run_caps(policy, documents)

    results: list[dict[str, Any]] = []
    produced: dict[int, str] = {}
    failed: set[int] = set()

    for ordinal, entry in enumerate(documents, start=1):
        index = ordinal - 1
        action = str(entry.get("action") or "create").lower()
        key = entry.get("idempotency_key") or ""
        doctype = entry.get("doctype") or ""
        payload = dict(entry.get("payload") or {})
        payload.pop("doctype", None)

        if not key:
            results.append(_result(entry, ordinal, "REJECTED",
                                   error_code="NO_IDEMPOTENCY_KEY",
                                   error_message="This document carried no idempotency key."))
            failed.add(index)
            continue

        if _references(payload, entry.get("docname")) & failed:
            results.append(_result(entry, ordinal, "SKIPPED",
                                   error_code="DEPENDENCY_FAILED",
                                   error_message="A document this one refers to was not written."))
            failed.add(index)
            continue

        replayed = get_write_log(env, key)
        if replayed.get("found") and replayed.get("status") == "COMMITTED":
            produced[index] = replayed.get("target_docname") or ""
            # A REPLAY IS STILL A DOCUMENT THE CUSTOMER HAS TO FIND. Read it
            # back so the receipt for "you already asked me to do this" names
            # the document and its company exactly as the first one did.
            existing = {}
            if replayed.get("target_docname"):
                existing = read_document_state(
                    env, doctype, str(replayed["target_docname"]),
                ) or {}
            results.append(_result(
                entry, ordinal, "REPLAYED",
                docname=replayed.get("target_docname") or "",
                docstatus=replayed.get("docstatus_written"),
                label=existing.get("label") or "",
                company=existing.get("company") or "",
                party=existing.get("party") or "",
            ))
            continue

        try:
            resolved = _resolve_references(payload, produced)
            # THE BACK-REFERENCE APPLIES TO THE DOCUMENT NAMED, NOT ONLY TO THE
            # VALUES INSIDE IT.
            #
            # "Record this entry and post it" is two items in one batch: a
            # create, and a submit whose `docname` is `@0` -- the document the
            # first item produced, whose id nothing can know until it has run.
            # Only the payload was being resolved, so the submit arrived asking
            # to post a document called "@0" and was refused with
            # "'@0' is not a record id". The entry was created and left sitting
            # in draft, while the customer had asked for it to be posted.
            entry = dict(entry)
            entry["docname"] = _resolve_references(
                {"docname": entry.get("docname")}, produced
            )["docname"]
            docname, docstatus, amount, label, company, party = _apply(
                env, action, doctype, resolved, entry, key, session_id, policy,
            )
        except AgentWriteError as exc:
            _record_log(env, idempotency_key=key, action=action, doctype=doctype,
                        payload=payload, session_id=session_id,
                        status="rejected", rejection_reason=exc.message)
            results.append(_result(entry, ordinal, "REJECTED",
                                   error_code=exc.code, error_message=exc.message))
            failed.add(index)
            continue
        except (UserError, ValidationError) as exc:
            # THIS SYSTEM'S OWN REFUSAL, in the sentence it wrote for a person.
            # `UserError` and `ValidationError` are what a model raises when it
            # means "somebody got this wrong", so the message is the accounting
            # reason and belongs in front of the customer unchanged.
            logger.info("Agent %s refused on %s: %s", action, doctype, exc)
            _record_log(env, idempotency_key=key, action=action, doctype=doctype,
                        payload=payload, session_id=session_id,
                        status="rejected", rejection_reason=_readable(exc))
            results.append(_result(entry, ordinal, "REJECTED",
                                   error_code="WRITE_REJECTED",
                                   error_message=_readable(exc)))
            failed.add(index)
            continue
        except Exception as exc:  # noqa: BLE001
            # A CRASH, NOT A REFUSAL, and the two readers are told different
            # things. Anything outside the user-facing family is the system
            # failing on this document rather than judging it, and its text is
            # Python talking to a programmer — the customer used to read
            # "unsupported operand type(s) for -: 'NoneType' and 'float'" as
            # the accounting reason their invoice was refused. They get a plain
            # sentence naming the document type; the operator gets the
            # exception's class and message on the log row and the whole
            # traceback in the server log.
            logger.exception("Agent %s failed on %s: %s", action, doctype, exc)
            _record_log(env, idempotency_key=key, action=action, doctype=doctype,
                        payload=payload, session_id=session_id,
                        status="failed",
                        error_details=f"{type(exc).__name__}: {exc}")
            results.append(_result(entry, ordinal, "REJECTED",
                                   error_code="WRITE_REJECTED",
                                   error_message=_could_not_process(doctype)))
            failed.add(index)
            continue

        produced[index] = docname
        results.append(_result(
            entry, ordinal,
            "CREATED" if action == "create" else "UPDATED",
            docname=docname, docstatus=docstatus, amount=amount,
            label=label, company=company, party=party,
        ))

    return {"results": results}
