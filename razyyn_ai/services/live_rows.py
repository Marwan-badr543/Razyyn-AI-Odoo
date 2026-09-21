# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Every table the agent reads is replaced by its live rows before the query runs.

WHAT WENT WRONG WITHOUT THIS
    The schema reply already says which rows of a model are real
    (`posted_filter`: `state = 'posted'`, `parent_state = 'posted'`). It said
    so as ADVICE, to a model that then wrote whatever SQL it liked. Forget the
    line, bury it in an OR, put it in the wrong subquery, and draft and
    cancelled moves enter the total. On the Frappe side a live audit reported
    148,500 of supplier payments "recorded twice" whose live total was 0.00 —
    every row behind the figure was cancelled, and the query that found them
    had no filter on it. Odoo's ledger fails the same way with `state`.

WHY THE TABLE IS REPLACED AND THE WHERE CLAUSE IS NOT INSPECTED
    Checking the model's WHERE is a war that is lost one query at a time: it
    forgets, it mistypes, it wraps the predicate in OR, it nests it in the
    wrong subquery. So the table itself is swapped for a filtered view of it,
    aliased to the same name:

        SELECT account_id, SUM(debit) FROM account_move_line WHERE ... GROUP BY ...

    becomes

        SELECT account_id, SUM(debit)
        FROM (SELECT * FROM "account_move_line" WHERE parent_state = 'posted')
             AS "account_move_line"
        WHERE ... GROUP BY ...

    The model's WHERE, JOIN, GROUP BY and subqueries all still work, because
    the alias keeps the original name; nothing written around the table can
    reach a dead row, because from the statement's point of view it does not
    exist; and it holds even when the model does not know the column exists.

WHY A PARSER, AND NOT A REGULAR EXPRESSION
    A table appears in a FROM, in a JOIN, inside a CTE, inside a derived
    table, on both sides of a UNION, and with or without an alias. The
    read-only guard gets away with regular expressions because "does this
    statement contain a forbidden word" is a question about the text; "which
    table references does this statement contain" is a question about the
    tree. `sqlglot` parses the statement, every `Table` node is visited, and
    the statement is written back in the same dialect.

IT FAILS OPEN, AND SAYS SO
    A statement the parser cannot read, or a site whose interpreter has no
    `sqlglot`, runs exactly as it did before this module existed — and the
    reply carries `live_rows_enforced: false`, so the caller knows the
    exclusion did not happen and can apply the filter itself. Refusing the
    query would restrict the agent; running it silently would restrict nobody
    and hide the gap. Neither is acceptable, so the third thing is done.

THIS IS A PORT, NOT AN INDEPENDENT DESIGN
    Mirrors the Frappe app's `agent_api/services/live_rows.py` line for line;
    only the dialect the caller passes differs. Pure on purpose: no `odoo`
    import. What a table's live rows are is asked of a callback, so
    `tests/test_live_rows.py` runs this file with no Odoo installed.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - the site has not installed it yet
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

#: Answers "which rows of this table are live" with a boolean SQL predicate
#: in the table's own columns, or None when every row of the table counts.
FilterFor = Callable[[str], Optional[str]]


def exclude_dead_rows(sql: str, dialect: str, filter_for: FilterFor) -> tuple:
    """The statement with every filtered table replaced by its live rows.

    Args:
        sql: the statement as the agent wrote it, already rewritten to the
            physical table names this site uses.
        dialect: the `sqlglot` dialect name — "postgres" for an Odoo site,
            "mysql" for a Frappe one.
        filter_for: what makes a row of a given table live, by table name.
            Returning None (or "") leaves that table exactly as written.

    Returns:
        ``(sql, applied, enforced)``. `applied` maps each table that was
        wrapped to the predicate it was wrapped with — empty when nothing
        needed wrapping. `enforced` is False only when the statement could
        not be parsed or the parser is not installed, in which case `sql` is
        returned untouched and the caller must say so.
    """
    if sqlglot is None:
        _logger.warning("sqlglot is not installed; cancelled rows are not excluded from this read.")
        return sql, {}, False

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # pylint: disable=broad-except
        _logger.warning("Could not parse a statement to exclude cancelled rows: %s", exc)
        return sql, {}, False

    applied = {}
    predicates = {}
    # Collected BEFORE any replacement: the filtered view that replaces a
    # table contains a Table node of its own, and that one must not be
    # wrapped again.
    tables = list(tree.find_all(exp.Table))

    try:
        for table in tables:
            name = table.name
            if not name:
                continue
            if name not in predicates:
                predicates[name] = _predicate_for(name, filter_for)
            predicate = predicates[name]
            if not predicate:
                continue
            table.replace(_live_view(table, predicate, dialect))
            applied[name] = predicate
        if not applied:
            return sql, {}, True
        return tree.sql(dialect=dialect), applied, True
    except Exception as exc:  # pylint: disable=broad-except
        _logger.warning("Could not rewrite a statement to exclude cancelled rows: %s", exc)
        return sql, {}, False


def _predicate_for(name: str, filter_for: FilterFor) -> Optional[str]:
    """One table's liveness predicate, or None — never an exception.

    The callback reads the registry on the customer's site; a table it
    cannot describe (a CTE alias, a junction table, a catalogue view) is a
    table whose every row counts.
    """
    try:
        predicate = filter_for(name)
    except Exception as exc:  # pylint: disable=broad-except
        _logger.debug("No liveness filter for %s: %s", name, exc)
        return None
    predicate = (predicate or "").strip()
    return predicate or None


def _live_view(table, predicate: str, dialect: str):
    """`(SELECT * FROM <table> WHERE <predicate>) AS <alias>`.

    The alias is the one the statement already used for the table, or the
    table's own name when it used none — so every column reference written
    against it still resolves.
    """
    alias = table.alias or table.name
    source = exp.Table(
        this=exp.to_identifier(table.name, quoted=True),
        db=table.args.get("db"),
        catalog=table.args.get("catalog"),
    )
    inner = (
        exp.Select()
        .select(exp.Star())
        .from_(source)
        .where(sqlglot.condition(predicate, dialect=dialect))
    )
    return exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))


def read_flag(value) -> bool:
    """A request parameter read as a boolean, however the transport spelled it.

    A JSON body arrives as a real boolean; form data arrives as the strings
    "true", "1" or "yes". Anything else — absent, "false", "0", "" — is
    False, which is the safe default: the exclusion is on unless it was asked
    to be off.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")
