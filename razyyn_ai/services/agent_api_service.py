# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Service layer — everything the controller needs, with an Odoo environment
in hand. Pure business logic; no HTTP status codes or request objects here
(``controllers/main.py`` owns that translation), matching the three-layer
convention the razyyn agent's own backend follows (project_rules.md).

Guard enforcement is delegated to ``query_guard.py`` — nothing about SQL
safety is re-decided in this file.
"""

from __future__ import annotations

import base64
import json
import difflib
import logging
import re
from datetime import timedelta

from odoo import fields as odoo_fields

from . import live_rows, sql_dialect
from .query_guard import (  # noqa: F401 (re-exported for the controller)
    ForbiddenQueryError,
    assert_query_is_read_only,
    is_denied_column,
)

_logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULT_ROWS = 500
DEFAULT_QUERY_TIMEOUT_SECONDS = 30

MAX_GENERATED_FILE_BYTES = 25 * 1024 * 1024
GENERATED_FILE_RETENTION_HOURS = 3


class MissingParameterError(Exception):
    def __init__(self, parameter_name: str) -> None:
        self.parameter_name = parameter_name
        super().__init__(f"Missing required parameter: {parameter_name}")


class ResourceNotFoundError(Exception):
    """Something was asked for by a name nothing here answers to.

    ``hint`` is where the answer goes when this connector knows it. A refusal
    that only says "not found" leaves the agent to guess the next name, and a
    guess is another round trip it pays for; one that names what DOES exist
    turns the retry into a correction.
    """

    def __init__(self, resource_type: str, identifier: str, hint: str = "") -> None:
        self.resource_type = resource_type
        self.identifier = identifier
        self.hint = hint
        message = f"{resource_type} '{identifier}' not found."
        super().__init__(f"{message} {hint}".strip())


class QueryExecutionError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"SQL Execution Error: {detail}")


class FileTooLargeError(Exception):
    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(f"File of {size_bytes} bytes exceeds the {limit_bytes} byte limit.")


# ─── Authentication ──────────────────────────────────────────────────────────


def authenticate_by_api_key(env, api_key):
    """Return the ``razyyn.agent.settings`` record for *api_key*, or ``None``.

    Security audit 2026-09, finding #1b: intentionally left on ``.sudo()``.
    This call establishes identity — it is what tells the caller *which*
    company's connection it holds — so there is no company (or user) to scope
    it to yet; every other ``.sudo()`` in this file runs after this one has
    already resolved an ``agent_settings`` record and is scoped from there.
    """
    return env["razyyn.agent.settings"].sudo().authenticate(api_key)


# ─── SQL Query Execution ─────────────────────────────────────────────────────


#: A dotted Odoo MODEL name written where a TABLE name belongs.
#:
#: Odoo models are dotted (``account.move``) and their tables are underscored
#: (``account_move``). ``get_schema`` shows the agent the dotted name, because
#: that is what an Odoo user calls the thing — so that is the name a model
#: reaches for when it writes ``FROM account.move``. PostgreSQL reads that as
#: schema ``account``, table ``move``, and answers "relation does not exist",
#: which says nothing about the actual mistake and which no amount of
#: re-prompting reliably teaches a model to avoid.
#:
#: This is the Odoo counterpart of the Frappe app's ``_rewrite_query``, which
#: exists for exactly the same reason: there, the agent may write ``FROM User``
#: and the app resolves it to ``tabUser``.
_MODEL_REFERENCE_PATTERN = re.compile(
    r"\b(from|join)\s+(\"?)([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+)\2",
    re.IGNORECASE,
)


def rewrite_model_names(env, query: str) -> str:
    """Resolve every dotted Odoo model name in *query* to its physical table.

    Only names that are ACTUALLY registered models on this database are
    rewritten. That is what keeps ``information_schema.columns`` — which
    matches the same dotted shape and which the audit sweep depends on —
    untouched, along with any genuinely schema-qualified table a customer's
    own database happens to carry.

    Runs BEFORE the guard, never after, for the same reason the Frappe app
    rewrites first: the denylist is written in PHYSICAL table names, so a
    statement naming this module's own credential store as
    ``razyyn.agent.settings`` must already read ``razyyn_agent_settings`` by
    the time the guard inspects it, or it slips past a check that was looking
    for the underscored spelling.
    """

    def replace(match: re.Match) -> str:
        keyword, quote, candidate = match.groups()
        if candidate not in env:
            return match.group(0)
        return f"{keyword} {env[candidate]._table}"

    return _MODEL_REFERENCE_PATTERN.sub(replace, query)


def validate_and_execute_query(env, sql_query: str,
                                max_rows: int = DEFAULT_MAX_RESULT_ROWS,
                                timeout_seconds: int = DEFAULT_QUERY_TIMEOUT_SECONDS,
                                include_cancelled: bool = False) -> dict:
    """Validate a SQL query for read-only safety, then execute it under limits.

    DRAFT AND CANCELLED ROWS ARE REMOVED FROM EVERY TABLE THE STATEMENT READS
        before it runs — see ``live_rows.py``. `include_cancelled` switches
        that off, for a caller whose question is about the cancelled
        documents themselves, and the reply records that it did. The reply
        also names what was excluded (`live_filters`) so the caller can state
        it, and says when the statement could not be parsed and ran as
        written (`live_rows_enforced` False) so the caller can filter itself.

    WHY THERE IS NO COMPANY FILTER HERE
        An earlier revision appended a company predicate to the agent's own
        SQL, and refused outright any query whose SELECT list did not project
        ``company_id`` so that such a predicate could be attached. That is
        removed, at the root, for two independent reasons.

        It did not work. ``SELECT id, name FROM res_partner LIMIT 3`` — an
        ordinary, correct question — came back refused, telling the model to
        add a column it had no reason to want. A model cannot satisfy that
        rule reliably, so it retries, is refused again, and the customer's
        question never gets answered. A guard that refuses correct queries is
        not a stricter guard; it is a broken endpoint.

        It could not have worked. Appending a predicate to arbitrary SQL means
        understanding that SQL, and this module deliberately carries no SQL
        parser (see ``query_guard.py``'s docstring). Its own predecessor
        documented the holes honestly — a leading CTE, a derived table, the
        second branch of a UNION, the second table of a JOIN all escaped the
        filter — so what it actually delivered was refusal of the simple
        queries and no scoping of the complicated ones.

        The posture now matches the Frappe app's, which has served production
        since it shipped: the boundary is READ-ONLY plus the guard's denylist,
        not a company predicate. A connection is to a customer's own database,
        which that customer owns in full; companies inside it are an
        accounting structure, not a tenancy boundary. The agent is told which
        company a question is about and scopes its own SQL accordingly — the
        company is a default, not a filter — and cross-company reads stay
        possible because real accounting work needs them (a shared chart of
        accounts, an intercompany reconciliation).

    Raises:
        MissingParameterError, ForbiddenQueryError (from query_guard),
        QueryExecutionError.
    """
    if not sql_query:
        raise MissingParameterError("sql_query")

    clean_query = rewrite_model_names(env, sql_query.strip().rstrip(";"))
    # Before the guard, never after, so what the guard inspects is exactly what
    # runs. The rewrite only ever wraps a column in COALESCE/`->>`; it adds no
    # table and no statement.
    clean_query = speak_this_database(env, clean_query)
    # AFTER the table names are physical and the columns speak this database,
    # BEFORE the guard — so what the guard inspects is exactly what runs. The
    # rewrite adds a SELECT * and a predicate in a table's own columns; it
    # adds no statement and no table.
    live_filters: dict = {}
    enforced = False
    if not include_cancelled:
        clean_query, live_filters, enforced = live_rows.exclude_dead_rows(
            clean_query, "postgres", lambda table: _live_filter_for_table(env, table),
        )
    assert_query_is_read_only(clean_query)  # raises ForbiddenQueryError

    cr = env.cr
    try:
        # A failing agent query (a bad column name — expected, and deliberately
        # forwarded below so the model can correct itself) would otherwise
        # abort `cr`'s whole transaction: every statement after it on this
        # cursor — including the response this function is building — would
        # raise InFailedSqlTransaction instead of returning the error the
        # agent needs to retry. A savepoint contains that to this query alone.
        with cr.savepoint():
            # Server-side statement timeout. SET LOCAL is savepoint/transaction
            # scoped, so it cannot leak onto whatever request reuses this
            # connection next.
            cr.execute("SET LOCAL statement_timeout = %s", (int(timeout_seconds) * 1000,))

            # Row cap enforced by the database via LIMIT on a wrapping query
            # rather than sliced in Python after the fact — a query that would
            # return ten years of ledger rows never materialises them past
            # max_rows + 1. The wrapper is valid whether or not the inner query
            # is itself a WITH ... SELECT.
            #
            # The inner query sits on its own line, between the wrapper's open
            # and close parens. Without that, a query ending in a line comment
            # (`SELECT ... -- note`) would comment out everything after it on
            # the same line — including this wrapper's closing `) AS ... LIMIT`
            # — and either break or, worse, silently drop the row cap.
            #
            # The cap is inlined as an integer and this execute is called with
            # NO parameter tuple, on purpose. Passing *any* parameters makes
            # psycopg2 %-interpolate the ENTIRE statement — the agent's own SQL
            # included — so an ordinary query carrying a percent sign
            # (`LIKE '%acme%'`, or `amount % 2`) has its `%` read as a
            # placeholder and dies with "tuple index out of range". max_rows is
            # a server-controlled int, never agent text, so inlining it is safe
            # and keeps psycopg2 out of the agent's SQL entirely.
            capped = f"SELECT * FROM (\n{clean_query}\n) AS razyyn_agent_capped LIMIT {int(max_rows) + 1}"
            # pylint: disable=sql-injection
            # False positive: clean_query already passed assert_query_is_read_only
            # above, and max_rows is int()-cast server config, never agent text —
            # see the comment block above for why this can't be parameterized.
            cr.execute(capped)
            rows = cr.dictfetchall()
    except ForbiddenQueryError:
        raise
    except Exception as exc:
        raise QueryExecutionError(_explain(env, clean_query, exc)) from exc

    # One row past the cap was fetched precisely so this is a fact rather than
    # an inference: a result of exactly max_rows rows is COMPLETE, and reporting
    # it as truncated would stop the agent aggregating a population it can
    # actually see in full.
    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]

    columns = list(rows[0].keys()) if rows else []
    return {
        "success": True,
        "columns": columns,
        "data": rows,
        "row_count": len(rows),
        "truncated": truncated,
        # The cap itself, not just the fact that it bound. A caller that knows
        # the page size can walk a wide GROUP BY in cursor-sized pages and
        # report the whole population; a caller that only knows "truncated" has
        # to guess, and a wrong guess either stops early (a partial figure
        # presented as a total) or asks for pages that do not exist.
        "max_rows": max_rows,
        # WHAT WAS EXCLUDED, BY NAME. A silent exclusion is as bad as none:
        # the caller has to be able to tell the reader "cancelled entries were
        # left out of this", and it can only say so if it is told.
        "live_filters": live_filters,
        "live_rows_enforced": enforced,
        "cancelled_included": bool(include_cancelled),
    }


def _live_filter_for_table(env, table_name: str):
    """Which rows of one physical table are live, for ``exclude_dead_rows``.

    The same answer ``build_schema_summary`` publishes as ``posted_filter``,
    read off the model's own ``state`` selection through ``_posting_state`` —
    so what the schema reply says and what a read actually excludes can never
    differ. A line model answers through its header's state, copied onto the
    line as ``parent_state``. A table that is no model's (a junction table, a
    CTE alias, a catalogue view) or a model with no notion of posting answers
    None: every row of it counts.
    """
    model = next(
        (env[name] for name, model_cls in env.registry.models.items()
         if getattr(model_cls, "_table", None) == table_name
         and not getattr(model_cls, "_abstract", False)),
        None,
    )
    if model is None:
        return None
    posting = _posting_state(env, model._name)
    if not posting:
        return None
    column, value, _values = posting
    return f"{column} = '{value}'"


# ─── Schema Summary ──────────────────────────────────────────────────────────


#: Relational field types that are NOT a column on the model's own table.
#: A one2many is the inverse of somebody else's column; a many2many lives in a
#: junction table. Both are still described below — the agent has to know where
#: an invoice's lines actually live — but described as what they are, never as
#: a column it could put in a SELECT list.
_NON_COLUMN_RELATIONS = {"one2many", "many2many"}

#: Layout-only field types with no data — excluded outright, the same reasoning
#: the Frappe app applies to Section/Column Break.
_IGNORED_FIELD_TYPES = {"separator"}

#: Value of a ``state`` column meaning "this document is real and counts".
#: The Odoo counterpart of the Frappe app's ``docstatus = 1``. A ledger total
#: computed without it sums drafts the accountant never posted and entries they
#: cancelled, and the figure looks perfectly reasonable.
_POSTED_STATE_VALUES = ("posted", "done", "sale", "purchase", "paid")


def _site_languages(env) -> tuple:
    """Which language keys to read a translated column under, best first.

    Odoo keeps the source term under `en_US` whatever language a record was
    entered in, so it is always the fallback and never the only key on a site
    that runs in something else.
    """
    site = (env.user.lang or env.context.get("lang") or "en_US")
    return (site,) if site == "en_US" else (site, "en_US")


def speak_this_database(env, query: str) -> str:
    """Rewrite the agent's SQL into the dialect this Odoo actually stores.

    See services/sql_dialect.py for what it does and why doing it is safe. In
    short: a text operator on a `jsonb` column and `->>` on a `varchar` one are
    both guaranteed errors, and the agent cannot tell which is which because
    `name` is jsonb on account_account and varchar on res_partner, on the same
    database, in the same query.
    """
    try:
        return sql_dialect.rewrite(
            query,
            lambda table: _column_types(env.cr, table),
            _site_languages(env),
        )
    except Exception as exc:
        # A query this cannot read is a query it must hand back untouched. The
        # database is still the one that decides whether it runs.
        _logger.warning("Razyyn AI: could not adapt a query to this database: %s", exc)
        return query


#: Odoo fields that look like columns and are not, with what to do instead.
#: Every one of these was selected by the agent against a customer's database
#: and came back "column does not exist".
#: Enough to correct a query without burying the reason it failed.
_MAX_COLUMNS_NAMED = 80

_NOT_A_COLUMN_HINTS = (
    ("property_", "a company-dependent property: it is not a column on this "
                  "table, it lives in ir_property as a row per company "
                  "(res_id = '<model>,<id>')"),
    ("display_name", "computed, never stored -- build it from the stored "
                     "columns instead"),
)


def _nearest_columns(wanted: str, columns: list) -> list:
    """The real columns most likely to be the one that was meant.

    Containment first and similarity second, because the misses that actually
    happen are of that shape: `type` on account_tax is `type_tax_use`, and
    `currency_name` on res_currency is `name`. Pure edit distance scores both
    of those below any sensible cut-off -- the first is barely half a match --
    while "one is inside the other" gets them instantly.
    """
    lowered = wanted.lower()
    contained = [
        column for column in columns
        if column != wanted and len(column) >= 4
        and (lowered in column.lower() or column.lower() in lowered)
    ]
    similar = [
        column for column in difflib.get_close_matches(wanted, columns, n=3, cutoff=0.5)
        if column not in contained and column != wanted
    ]
    return (contained + similar)[:3]


def _explain(env, query: str, exc: Exception) -> str:
    """The database's complaint, plus what the agent needs to fix it.

    A bare "column \"code\" does not exist" leaves the agent to guess again,
    and a customer's log showed it guessing eight times in one session. The
    columns the table really has are one lookup away, and naming them turns a
    retry into a correction.
    """
    message = str(exc)
    missing = re.search(r'column "?([A-Za-z_][\w.]*)"? does not exist', message)
    if not missing:
        return message

    column = missing.group(1).split(".")[-1]
    lines = [message.strip()]

    explained = False
    for prefix, advice in _NOT_A_COLUMN_HINTS:
        if column.startswith(prefix) or column == prefix:
            lines.append(f"`{column}` is {advice}.")
            explained = True
            break

    try:
        for name, table in sorted(sql_dialect.tables_in(query).items()):
            if name != table:
                continue
            columns = sorted(_actual_columns(env.cr, table))
            if not columns:
                continue

            # The nearest real name first, because that is usually the whole
            # fix -- `type` on account_tax is `type_tax_use`, and the agent
            # spent eight attempts not finding that out.
            near = [] if explained else _nearest_columns(column, columns)
            if near:
                lines.append(f"{table} has no `{column}`. Did you mean: "
                             f"{', '.join(near)}?")
            shown = columns[:_MAX_COLUMNS_NAMED]
            more = "" if len(columns) == len(shown) else f" (+{len(columns) - len(shown)} more)"
            lines.append(f"{table} really has: {', '.join(shown)}{more}")
    except Exception as exc:
        _logger.debug("Razyyn AI: could not build a 'did you mean' hint: %s", exc)

    return "\n".join(lines)


def _posting_state(env, model_name: str):
    """How this model says "posted", or ``None`` if it has no such notion.

    Returns ``(column, value, all_values)``. Derived from the model's own
    ``state`` selection rather than from a table of model names, because
    customers install modules this connector has never heard of and a
    hard-coded list silently says "not submittable" for every one of them —
    which reads to the agent as "count every row", the same error as ignoring
    docstatus on ERPNext.
    """
    model = env[model_name]
    field = model._fields.get("state")
    if field is None or field.type != "selection":
        return None

    try:
        values = [value for value, _label in field._description_selection(env)]
    except Exception:
        return None

    for candidate in _POSTED_STATE_VALUES:
        if candidate in values:
            return "state", candidate, values
    return None


def _actual_columns(cr, table_name: str) -> set:
    """The columns this table REALLY has, asked of the database itself.

    WHY NOT INFER IT FROM THE FIELD DEFINITION
        Because every inference so far has been wrong in a new way. Filtering
        on ``store`` removed the computed fields and still left
        ``invoice_pdf_report_file`` — a binary field declared with
        ``attachment=True``, which Odoo stores in ``ir.attachment`` rather than
        in a column while reporting ``store=True`` quite truthfully. The next
        Odoo release will have another such case, and the symptom will be the
        same: the agent selects a column it was told about, gets "column does
        not exist", and has no way to know which of the remaining names are
        also fictional.

        The database already knows the answer exactly. One indexed lookup
        against ``information_schema`` per schema call — and the agent caches
        each schema for minutes — is a small price for a field list that cannot
        be wrong.
    """
    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table_name,),
    )
    return {row[0] for row in cr.fetchall()}


def _column_types(cr, table_name: str) -> dict:
    """Column name -> its real PostgreSQL type, for this table."""
    cr.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table_name,),
    )
    return {row[0]: row[1] for row in cr.fetchall()}


def _translated_column_sql(env, column: str) -> str:
    """How to read a translated column in raw SQL, on THIS site.

    WHY THE AGENT HAS TO BE TOLD
        Odoo does not store a translatable field as text. `account_account.name`
        is a `jsonb` object keyed by language, so the obvious query --
        `WHERE name ILIKE '%Bank%'` -- does not return nothing, it FAILS:
        "operator does not exist: jsonb ~~* unknown". The agent was looking up
        the schema first, exactly as instructed, being told the field was a
        `char`, and then watching its query be rejected by the database. Three
        of those in a row is how "the agent cannot create anything in Odoo"
        looks from the outside.

        The schema is the only place that knows, so the schema says it, as the
        expression to paste rather than as a rule to remember.

    The site's own language comes first and English is the fallback, because
    Odoo keeps the source term under `en_US` whatever language the data was
    entered in -- so this reads correctly on a site where a given record has
    never been translated.
    """
    site_lang = (env.user.lang or env.context.get("lang") or "en_US")
    keys = [site_lang] if site_lang == "en_US" else [site_lang, "en_US"]
    reads = [f"{column}->>'{key}'" for key in keys]
    if len(reads) == 1:
        return reads[0]
    return f"COALESCE({', '.join(reads)})"


def resolve_model_name(env, wanted: str) -> str:
    """The technical model name for whatever the agent called it.

    WHY THIS EXISTS
        A customer's log: ``Model 'Journal Entry' not found.`` "Journal Entry"
        is not a mistake — it is precisely what Odoo itself calls
        ``account.move``, on the menu, in the breadcrumb and in ``ir.model``.
        The agent had read the words off the customer's own screen. A connector
        that answers a 404 to the name its own interface prints is refusing on
        a technicality, and the desk has nowhere to go from there: it guesses
        another spelling, is refused again, and reports that the customer's
        accounting system has no journal entries.

        So a model is found the way a person finds it — by the name it goes by
        — and the TECHNICAL name is what comes back, so everything downstream
        speaks the database's own spelling from then on. That is the same rule
        the write side already follows: the system publishes its own word.

    The order is exact-technical first, because that is the common case and
    must never be slowed down or second-guessed by a label search.
    """
    wanted = (wanted or "").strip()
    if not wanted:
        raise MissingParameterError("model")

    if wanted in env:
        return wanted

    models = env["ir.model"].sudo()

    # The same name with the other separator: "account move" and "account_move"
    # are both things a model writes for `account.move`.
    dotted = re.sub(r"[\s_]+", ".", wanted.lower())
    if dotted in env:
        return dotted

    # By the label Odoo shows, case-insensitively. `ir.model.name` is
    # translated, so this matches whatever language the site runs in. The
    # singular is tried too: a model asking about several documents writes
    # "Journal Entries", and the label is "Journal Entry".
    for candidate in (wanted, _singular(wanted)):
        if not candidate:
            continue
        for field in ("name", "model"):
            found = models.search([(field, "=ilike", candidate)], limit=1)
            if found and found.model in env:
                return found.model

    raise ResourceNotFoundError("Model", wanted, _model_suggestions(env, wanted))


def _singular(word: str) -> str:
    """"Journal Entries" -> "Journal Entry". Plain English, which is what
    ir.model labels are written in even on a translated site."""
    lowered = word.strip()
    if lowered.lower().endswith("ies"):
        return lowered[:-3] + "y"
    if lowered.lower().endswith("ses"):
        return lowered[:-2]
    if lowered.lower().endswith("s") and not lowered.lower().endswith("ss"):
        return lowered[:-1]
    return ""


def _model_suggestions(env, wanted: str) -> str:
    """What this Odoo DOES have that is close to the name asked for.

    Searched over both spellings a model can be asked for — the technical name
    and the label — because the agent may have either half right.
    """
    words = [w for w in re.split(r"[\s._]+", wanted.lower()) if len(w) > 2]
    if not words:
        return ""

    leaves = []
    for word in words:
        leaves.append(("model", "ilike", word))
        leaves.append(("name", "ilike", word))
    # One "|" fewer than there are leaves: Odoo's domains are prefix notation,
    # and an operator count that does not match the leaves is not a narrower
    # search, it is a parse error — which this used to swallow, so the hint
    # that makes the refusal useful silently never appeared.
    domain = ["|"] * (len(leaves) - 1) + leaves
    try:
        candidates = env["ir.model"].sudo().search(domain, limit=40)
    except Exception:
        _logger.warning("Could not search models like %r", wanted, exc_info=True)
        return ""

    def relevance(record):
        """Most words matched first, then the shortest name.

        Unranked, Odoo returns them in its own order and the nearest model can
        sit fifth behind three that share one word — which is the difference
        between a hint the agent acts on and a list it ignores.
        """
        technical = record.model.lower()
        label = (record.name or "").lower()
        matched = sum(1 for w in words if w in technical or w in label)
        in_name = sum(1 for w in words if w in technical)
        return (-matched, -in_name, len(technical))

    named = [
        f"{record.model} ({record.name})"
        for record in sorted(
            (r for r in candidates if r.model in env), key=relevance,
        )
    ][:5]
    if not named:
        return (
            "Nothing in this system is named anything like that. Ask for the "
            "model by its technical name, such as account.move."
        )
    return "This system has: " + "; ".join(named) + "."


def _where_a_related_field_lives(env, model_name: str, field_name: str, declared) -> str:
    """One related field, as the join that reaches it — or "" if it cannot be.

    Only the ONE-HOP case is described, because only that one is a join the
    agent can write without being told anything else: this table holds a
    reference, and the value is a column on the table it points at. A two-hop
    path would need every table between named, and a sentence nobody can act on
    is worse than saying nothing.
    """
    # Odoo holds the path as a dotted STRING ("product_tmpl_id.name"), not a
    # sequence. Iterating it as one turns a two-hop path into thirty-one
    # single characters and this said nothing at all about anything.
    path = str(getattr(declared, "related", "") or "").split(".")
    if len(path) != 2:
        return ""
    link_name, target_field = path
    link = env[model_name]._fields.get(link_name)
    if link is None or link.type != "many2one" or not link.comodel_name:
        return ""
    if link.comodel_name not in env:
        return ""
    target = env[link.comodel_name]
    here = env[model_name]._table
    reached_by = f"{here}.{link_name} = {target._table}.id"

    if target_field in _actual_columns(env.cr, target._table):
        return (f"{field_name}: NOT a column here — it is "
                f"{target._table}.{target_field}, reached by {reached_by}")

    # THE ONE THAT KEPT BEING SELECTED ANYWAY. A product variant's taxes are
    # its template's taxes, and its template's taxes are a many-to-many. So the
    # name is a column on NEITHER table, and a reply that simply omitted it
    # left an agent to write `pt.taxes_id` over and over.
    onwards = target._fields.get(target_field)
    if onwards is not None and onwards.type == "many2many" and onwards.comodel_name:
        junction = getattr(onwards, "relation", "") or ""
        rows = env[onwards.comodel_name]._table if onwards.comodel_name in env else ""
        if junction and rows:
            return (f"{field_name}: NOT a column anywhere — it is "
                    f"{target._table}.{target_field}, itself a many-to-many "
                    f"onto {rows} through {junction} "
                    f"({junction}.{getattr(onwards, 'column1', '?')} = "
                    f"{target._table}.id, {junction}."
                    f"{getattr(onwards, 'column2', '?')} = {rows}.id); "
                    f"reach the row first by {reached_by}")
    return ""


def build_schema_summary(env, model_name: str, agent_settings=None) -> dict:
    """Field summary for one Odoo model, shaped for the agent to write SQL from
    — the Odoo counterpart of the Frappe app's ``build_doctype_schema_summary``.

    ONLY REAL COLUMNS ARE PRESENTED AS COLUMNS
        Odoo declares far more fields than it stores. On a stock Odoo 17,
        ``res.partner`` has 153 declared fields and 81 actual columns: the rest
        are computed, related or otherwise non-stored, and exist only in
        Python. An earlier revision listed all 153, so the agent was handed 72
        column names for ``res_partner`` that no SELECT can ever return —
        ``activity_state``, ``access_url``, ``active_lang_count`` and the like.
        Every query built on one of them fails with "column does not exist",
        the agent has no way to know which of the names it was GIVEN are real,
        and it retries with another phantom. That is the single most damaging
        way this endpoint can be wrong, because it fails on the agent's own
        best behaviour: it looked the schema up first, exactly as instructed.

        So the list below is filtered to ``store``d fields. Relations that are
        not columns are still described, because the agent must be able to find
        where an invoice's lines live — but they are labelled as the joins they
        are, with the table and the linking column named, so they can never be
        mistaken for something to select.

    Raises:
        MissingParameterError, ResourceNotFoundError.
    """
    # Whatever it was called, this is what the database calls it. Everything
    # below — and everything the agent writes afterwards — uses this name.
    model_name = resolve_model_name(env, model_name)

    real_columns = _actual_columns(env.cr, env[model_name]._table)
    column_types = _column_types(env.cr, env[model_name]._table)
    ir_model_fields = env["ir.model.fields"].sudo()
    field_records = ir_model_fields.search([("model", "=", model_name)], order="name")

    columns: list[str] = ["id (integer, primary key)"]
    joins: list[str] = []
    properties: list[str] = []

    for field in field_records:
        if field.ttype in _IGNORED_FIELD_TYPES:
            continue

        if field.ttype in _NON_COLUMN_RELATIONS:
            # Not selectable. Named as a join so the agent can reach the rows.
            if field.ttype == "one2many" and field.relation and field.relation_field:
                target = env[field.relation]._table if field.relation in env else field.relation.replace(".", "_")
                joins.append(
                    f"{field.name}: the rows live in table {target}, "
                    f"one row per line, linked by {target}.{field.relation_field} = "
                    f"{env[model_name]._table}.id"
                )
            elif field.ttype == "many2many" and field.relation:
                target = env[field.relation]._table if field.relation in env else field.relation.replace(".", "_")
                # ASKED OF THE MODEL WHEN THE DEFINITION ROW IS SILENT. Odoo
                # fills `relation_table` in on the field object always and on
                # `ir.model.fields` only sometimes, so "(a junction table)"
                # reached the agent for fields whose junction has a perfectly
                # good name — and it then wrote the join against a table it had
                # invented.
                declared_m2m = env[model_name]._fields.get(field.name)
                junction = (field.relation_table
                            or getattr(declared_m2m, "relation", "")
                            or "(a junction table)")
                ends = ""
                if declared_m2m is not None and junction != "(a junction table)":
                    here = env[model_name]._table
                    ends = (f" ({junction}."
                            f"{getattr(declared_m2m, 'column1', '?')} = {here}.id, "
                            f"{junction}."
                            f"{getattr(declared_m2m, 'column2', '?')} = {target}.id)")
                joins.append(
                    f"{field.name}: many-to-many onto {target} through "
                    f"{junction}{ends}"
                )
            continue

        if field.name not in real_columns:
            # Declared by the model but absent from the table: computed,
            # related, or stored somewhere other than a column. Listing it
            # would hand the agent a name no SELECT can return.
            #
            # ONE KIND OF ABSENCE IS WORTH EXPLAINING RATHER THAN HIDING. A
            # company-dependent field — a partner's receivable account, a
            # product's cost, a category's income account — is a real,
            # per-company value that simply does not live in this table. Left
            # out in silence, an agent looking for one reads the list, does not
            # find it, and tries `property_account_receivable_id` anyway,
            # then `property_account_position_id`, then
            # `property_payment_term_id` — each refused, each time told only
            # what the table DOES have, which is never where the answer is.
            # Named here, with where it actually lives, the question has an
            # answer the first time it is asked.
            # ASKED OF THE MODEL, NOT OF ITS DEFINITION ROW. `ir.model.fields`
            # has no `company_dependent` column — the flag lives on the ORM
            # field object — so reading it off the definition silently answered
            # False for every field there is.
            declared = env[model_name]._fields.get(field.name)
            if getattr(declared, "company_dependent", False):
                properties.append(
                    f"{field.name} ({field.ttype})"
                    + (f" - {field.field_description}" if field.field_description else "")
                )
                continue
            # A FIELD THAT IS REALLY A COLUMN ON ANOTHER TABLE. Odoo's
            # `related` fields are the commonest shape of this: a product
            # variant's `name` is its template's `name`, and it is not stored
            # on `product_product` at all. Omitted in silence, the agent reads
            # the column list, sees no `name`, and selects it anyway — because
            # every product in the world has a name and its absence reads as an
            # oversight. Told where the value actually sits, and how to reach
            # it, the same query is written correctly the first time.
            elsewhere = _where_a_related_field_lives(env, model_name, field.name, declared)
            if elsewhere:
                joins.append(elsewhere)
            continue

        if is_denied_column(field.name):
            # A real column, but one the SQL guard refuses — a credential, a
            # token, a tax or bank identifier. Naming it here would be an
            # invitation the guard then declines: the agent writes a query
            # selecting a column it was explicitly told about, is refused, and
            # cannot tell which of the other names are also off-limits.
            continue

        info = f"{field.name} ({field.ttype})"
        if field.field_description:
            info += f" - {field.field_description}"
        if field.required:
            info += " [Required]"
        if column_types.get(field.name) == "jsonb":
            # TWO DIFFERENT REASONS A COLUMN IS JSON HERE, AND THEY ARE READ
            # DIFFERENTLY. A translated column is keyed by LANGUAGE. A
            # company-dependent one — which newer Odoo keeps on the table
            # instead of in a properties table — is keyed by COMPANY ID.
            #
            # Described as translated, a partner's credit limit and receivable
            # account came back with `-> read it as credit_limit->>'en_US'`,
            # which returns nothing at all: the keys are "1" and "2". The agent
            # then reads an empty value for a field it was told how to read,
            # which is worse than being told nothing.
            if getattr(env[model_name]._fields.get(field.name), "company_dependent", False):
                info += (
                    f" [per-company column, stored as JSON keyed by COMPANY ID "
                    f"-- in SQL read this company's value as "
                    f"{field.name}->>'<the company id>', never ->>'en_US']"
                )
            else:
                info += (
                    f" [translated column, stored as JSON per language -- "
                    f"in SQL read it as {_translated_column_sql(env, field.name)}]"
                )
        if field.ttype == "many2one" and field.relation:
            target = env[field.relation]._table if field.relation in env else field.relation.replace(".", "_")
            info += f" -> holds the id of a row in {target} ({field.relation})"
        if field.ttype == "selection" and field.selection:
            info += f" [Options: {field.selection}]"
        columns.append(info)

    summary = {
        "success": True,
        "model": model_name,
        "table_name": env[model_name]._table,
        "fields": columns,
        # Named separately from `fields` so neither can be mistaken for the
        # other. Everything in `fields` may go in a SELECT list; nothing in
        # `related_tables` may.
        "related_tables": joins,
        # Per-company values that are NOT columns anywhere on this table.
        "company_dependent_values": properties,
        "note": (
            "`fields` lists every column that exists on this table — computed "
            "and related fields Odoo does not store are deliberately omitted, "
            "because selecting one fails. If a name you expected is not in "
            "`fields`, it is not a column here: look for it under "
            "`related_tables` or `company_dependent_values`, which say where "
            "it really is, and NEVER select it from this table anyway. Rows "
            "belonging to this record live "
            "in the tables named under `related_tables`. A field marked "
            "`translated column` is stored as JSON keyed by language: use the "
            "expression given with it, in the SELECT list and in WHERE alike — "
            "comparing the bare column to text is refused by the database. "
            "Anything under `company_dependent_values` is a per-company value "
            "held in the ir_property table, not a column here: read it with "
            "`SELECT value_reference FROM ir_property WHERE name = '<the "
            "field>' AND res_id = '<this model>,<the row id>'` (a row with a "
            "NULL res_id is the company-wide default). Before reaching for "
            "one, check whether you need it at all — when you are preparing a "
            "document, this system fills these in for itself."
        ),
    }

    posting = _posting_state(env, model_name)
    if posting:
        column, value, values = posting
        # The vendor-neutral fact the agent needs before it totals anything.
        # Its ERPNext counterpart is `docstatus = 1`; here it is whatever this
        # model's own state selection calls "posted".
        summary["posted_filter"] = f"{column} = '{value}'"
        summary["state_values"] = values

    return summary


# ─── Session ownership (IDOR guard) ──────────────────────────────────────────


def get_or_create_session(env, session_id: str, agent_settings):
    # The boundary on this method is the ownership check a few lines down
    # (agent_settings_id == the caller's own connection). It is what stops one
    # valid API key from reading or writing into another connection's session,
    # and it is unrelated to company scoping.
    Session = env["razyyn.agent.chat.session"].sudo()
    session = Session.search([("session_id", "=", session_id)], limit=1)
    if session:
        if session.agent_settings_id.id != agent_settings.id:
            # Exists, but not this caller's — same "not found" answer as a
            # session that never existed, so this endpoint cannot be used to
            # enumerate another connection's session ids by response alone.
            raise ResourceNotFoundError("Chat session", session_id)
        session.touch()
        return session
    return Session.create({"session_id": session_id, "agent_settings_id": agent_settings.id})


# ─── Generated file upload ───────────────────────────────────────────────────


def save_generated_file(env, session_id: str, filename: str, content: bytes, agent_settings) -> dict:
    if not session_id:
        raise MissingParameterError("session_id")
    if len(content) > MAX_GENERATED_FILE_BYTES:
        raise FileTooLargeError(len(content), MAX_GENERATED_FILE_BYTES)

    session = get_or_create_session(env, session_id, agent_settings)

    attachment = env["ir.attachment"].sudo().create({
        "name": filename,
        # `datas` is base64 IN, not raw bytes — ir.attachment's own inverse
        # b64-decodes whatever it is given; raw bytes here corrupt the file
        # or raise, they are not encoded a second time for you.
        "datas": base64.b64encode(content),
        # Attached to the session, which is what makes this file reachable
        # through the chat's own download route and what scopes the hourly
        # cleanup sweep. Never `public`: these are trial balances and audit
        # working papers, and a public ir.attachment in Odoo is served to
        # anonymous callers by URL alone.
        "res_model": "razyyn.agent.chat.session",
        "res_id": session.id,
        "public": False,
    })

    # NO chat message is written here, and that is parity, not an omission.
    # The Frappe app's save_generated_file stores the File and returns its
    # URL; the agent then embeds that URL in its OWN reply as a
    # `[FILE:name:url]` chip (agent/tools/document_generator.py). A message
    # written here as well would render the same file to the customer twice.
    return {
        "success": True,
        # The caller — document_generator.py — refuses the upload outright
        # when this key is absent ("Upload succeeded but no file URL
        # returned"), so the file it just spent a minute producing never
        # reaches the customer. It is the chat's own authenticated download
        # route rather than Odoo's /web/content, because the person reading
        # the chat is authenticated by this module's chat login, not
        # necessarily by an Odoo back-office session.
        "file_url": f"/razyyn/chat/download_file?attachment_id={attachment.id}",
        "filename": filename,
        "attachment_id": attachment.id,
    }


def cleanup_old_files(env) -> int:
    """Hourly sweep of expired agent-generated reports — the Odoo counterpart
    of the Frappe app's ``cleanup_old_files``. Scoped to attachments this
    module itself created (``res_model`` is this module's own session model),
    so nothing an accountant uploaded elsewhere in Odoo is ever at risk here.
    """
    cutoff = odoo_fields.Datetime.now() - timedelta(hours=GENERATED_FILE_RETENTION_HOURS)
    expired = env["ir.attachment"].sudo().search([
        ("res_model", "=", "razyyn.agent.chat.session"),
        ("create_date", "<", cutoff),
    ], limit=500)
    count = len(expired)
    expired.unlink()
    return count
