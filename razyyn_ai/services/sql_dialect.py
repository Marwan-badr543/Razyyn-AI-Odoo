# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Making a correct-looking query run against the way Odoo really stores names.

THE PROBLEM, WHICH NO AMOUNT OF PROMPTING FIXES
    Odoo stores a translatable field as `jsonb`, keyed by language. It stores a
    non-translatable one as `varchar`. Whether a given column is one or the
    other is not a property of Odoo, it is a property of the MODEL -- and on a
    stock Odoo 17 `name` is:

        jsonb    on account_account, account_tax, product_template
        varchar  on res_partner, product_category, res_currency, res_company,
                 ir_sequence

    A query cannot be written to suit both. `WHERE name ILIKE '%bank%'` does
    not return nothing against a jsonb column, it FAILS -- "operator does not
    exist: jsonb ~~* unknown". And the obvious correction, `name->>'en_US'`,
    fails just as hard the other way -- "operator does not exist: character
    varying ->> unknown".

    A customer's log showed the agent oscillating between the two for a whole
    session: ILIKE on product_template, corrected to ->> on product_category,
    corrected back, over and over, never answering the question. It is not a
    reasoning failure. The information is not derivable from anything the model
    can see, and the schema summary only covers the model it asked about, not
    the four others its join reaches.

WHAT THIS DOES
    Reads the real column types out of the database and rewrites the query so
    it runs. Nothing else.

WHAT IT PROMISES, EXACTLY -- AND IT IS TWO DIFFERENT PROMISES
    MOST of what it touches CANNOT EXECUTE as written. A pattern operator
    applied to jsonb has no operator at all; `->>` applied to varchar has none
    either; `= 'Bank'` against jsonb has none. For those, a rewrite can only
    turn a certain failure into a result, and it cannot change the meaning of a
    query that would have run.

    TWO of them deliberately change a query that DOES run, because what it
    returns is wrong rather than impossible:

      * `SELECT name` on a translated column succeeds and hands back
        `{"en_US": "Bank"}`. Nothing complains, and a customer reads the JSON
        in their answer.
      * `ORDER BY name` on one sorts by the raw JSON text, so the rows come
        back in an order that has nothing to do with the names on the screen.

    Do not extend this module on the strength of the first promise alone.

    Where jsonb genuinely supports the operator -- `a.name = b.name` joining
    two translated columns, or `name IN (SELECT ...)` -- the query runs today
    and is LEFT ALONE. Only a comparison against a string constant is
    corrected, because only that one is certain to fail.

    Anything it is not sure about it leaves exactly as it found it, and the
    database's own error goes back to the agent as before. It does not touch
    string literals either: they are masked before any pattern runs, so a query
    searching for the text "name ILIKE" is unaffected.

No Odoo imports: the rules are testable on their own, and are.
"""

from __future__ import annotations

import re

#: What a SELECT list item may be, when it is just a column.
_PLAIN_ITEM = re.compile(
    r"^\s*(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s*$"
)
_ALIASED_ITEM = re.compile(
    r"^\s*(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s+(?:[Aa][Ss]\s+)?([A-Za-z_]\w*)\s*$"
)

#: `FROM x`, `JOIN x AS y`. The alias is optional and must not be a keyword --
#: `FROM account_move WHERE` would otherwise read "WHERE" as an alias.
_TABLE_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:ONLY\s+)?([A-Za-z_]\w*)"
    r"(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?",
    re.IGNORECASE,
)

_NOT_AN_ALIAS = {
    "as", "on", "using", "where", "group", "order", "having", "limit", "offset",
    "union", "intersect", "except", "left", "right", "inner", "outer", "full",
    "cross", "natural", "join", "select", "window", "fetch", "for", "lateral",
    "returning", "with",
}

#: Operators jsonb does not have AT ALL. A translated column on the left of one
#: of these is a guaranteed error, so wrapping it can only help.
_PATTERN_OPERATOR = re.compile(
    r"(?:(?P<qualifier>[A-Za-z_]\w*)\.)?(?P<column>[A-Za-z_]\w*)"
    r"(?!\s*(?:->>|->|\())"
    r"\s*(?P<operator>(?:NOT\s+)?I?LIKE\b|!?~~\*?|(?:NOT\s+)?SIMILAR\s+TO\b)",
    re.IGNORECASE,
)

#: Equality and IN, which jsonb DOES have -- `a.name = b.name` between two
#: translated columns is valid SQL that runs today. So these are only rewritten
#: when the other side is a string constant, where the comparison is certain to
#: fail as written. Anything else is left alone.
_EQUALITY_TO_A_LITERAL = re.compile(
    r"(?:(?P<qualifier>[A-Za-z_]\w*)\.)?(?P<column>[A-Za-z_]\w*)"
    r"(?!\s*(?:->>|->|\())"
    r"\s*(?P<operator>=|<>|!=)\s*(?=\x00\d+\x00)",
    re.IGNORECASE,
)

_IN_A_LIST_OF_LITERALS = re.compile(
    r"(?:(?P<qualifier>[A-Za-z_]\w*)\.)?(?P<column>[A-Za-z_]\w*)"
    r"(?!\s*(?:->>|->|\())"
    r"\s*(?P<operator>(?:NOT\s+)?IN\s*\()"
    r"(?=\s*\x00\d+\x00(?:\s*,\s*\x00\d+\x00)*\s*\))",
    re.IGNORECASE,
)

#: `col->>'lang'`, after literals have been masked.
_JSON_READ = re.compile(
    r"(?:(?P<qualifier>[A-Za-z_]\w*)\.)?(?P<column>[A-Za-z_]\w*)"
    r"\s*->>\s*(?P<literal>\x00\d+\x00)"
)

_SORT_CLAUSE = re.compile(r"\b(ORDER\s+BY|GROUP\s+BY)\b", re.IGNORECASE)
_CLAUSE_END = re.compile(
    r"\b(?:LIMIT|OFFSET|FETCH|HAVING|WINDOW|UNION|INTERSECT|EXCEPT|ORDER\s+BY)\b",
    re.IGNORECASE,
)

JSONB = "jsonb"
#: A jsonb column keyed by COMPANY ID rather than by language - Odoo 18's
#: `account_account.code_store` is one. Reported by the caller's type map
#: (the database says only "jsonb"; the model says which kind), and read
#: with the connection's company rather than a language. Without the
#: distinction every rewrite below applied `->>'en_US'` to it and the
#: query ran - returning nothing at all for every account.
JSONB_COMPANY = "jsonb_company"
JSON_KINDS = (JSONB, JSONB_COMPANY)


# ─── Keeping string literals out of it ───────────────────────────────────────

_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def _mask(sql: str) -> tuple[str, list[str]]:
    """Replace every literal and quoted identifier with a placeholder.

    Without this, a query that legitimately searches for the words it is being
    inspected for -- `WHERE note ILIKE '%name = %'` -- gets rewritten inside
    its own search term.
    """
    kept: list[str] = []

    def take(match: re.Match) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    return _LITERAL.sub(take, sql), kept


def _unmask(sql: str, kept: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], sql)


# ─── What the query is talking about ─────────────────────────────────────────

def _depths(sql: str) -> list[int]:
    """Parenthesis depth at every position, so a subquery can be told apart."""
    out, depth = [], 0
    for character in sql:
        if character == "(":
            out.append(depth)
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
            out.append(depth)
        else:
            out.append(depth)
    return out


_MORE_TABLES = re.compile(
    r"\s*,\s*([A-Za-z_]\w*)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?"
)


def tables_in(sql: str, top_level_only: bool = False) -> dict[str, str]:
    """Every name the query can use for a table -> the real table.

    The table's own name is always a key, so an unaliased `FROM res_partner`
    still answers `res_partner.name`.

    `top_level_only` keeps just the tables of the outermost query. That is what
    an unqualified column in the outer SELECT list resolves against in real
    SQL, and without the distinction a subquery that happens to mention
    `res_partner` makes the outer `name` look ambiguous and nothing gets
    rewritten at all.
    """
    depth_at = _depths(sql)
    found: dict[str, str] = {}

    def keep(table, alias, depth):
        if top_level_only and depth != 0:
            return
        found[table] = table
        if alias and alias.lower() not in _NOT_AN_ALIAS:
            found[alias] = table

    for match in _TABLE_REFERENCE.finditer(sql):
        depth = depth_at[match.start()] if match.start() < len(depth_at) else 0
        keep(match.group(1), match.group(2), depth)

        # `FROM a, b c` -- the old comma-separated join. The second table was
        # invisible, so a column both tables have looked unambiguous and was
        # rewritten against the wrong one.
        at = match.end()
        while True:
            more = _MORE_TABLES.match(sql, at)
            if not more:
                break
            keep(more.group(1), more.group(2),
                 depth_at[more.start(1)] if more.start(1) < len(depth_at) else 0)
            at = more.end()

    return found


class _Schema:
    """The column types of everything this query names, asked once each."""

    def __init__(self, sql: str, types_for_table):
        cache: dict[str, dict] = {}

        def columns_of(table):
            if table not in cache:
                cache[table] = {
                    column: str(kind).lower()
                    for column, kind in (types_for_table(table) or {}).items()
                }
            return cache[table]

        self.by_name = {n: columns_of(t) for n, t in tables_in(sql).items()}
        self.outer = {n: columns_of(t) for n, t in tables_in(sql, True).items()}

    def type_of(self, qualifier, column):
        """The column's type, or None when it cannot be said for certain.

        An unqualified name is only answered when exactly ONE table has such a
        column -- the outermost query's tables first, because that is what SQL
        itself resolves against, and everything the query mentions after that.
        Two candidates would be ambiguous to Postgres as well, so guessing
        could only ever pick the wrong one.
        """
        if qualifier:
            return self.by_name.get(qualifier, {}).get(column)

        for scope in (self.outer, self.by_name):
            seen = [
                columns[column]
                for columns in _distinct(scope)
                if column in columns
            ]
            if len(seen) == 1:
                return seen[0]
            if len(seen) > 1:
                return None
        return None

    def columns_of(self, name):
        return sorted(self.by_name.get(name, {}))


def _distinct(scope):
    out, seen = [], set()
    for columns in scope.values():
        marker = id(columns)
        if marker not in seen:
            seen.add(marker)
            out.append(columns)
    return out


# ─── The rewrite ─────────────────────────────────────────────────────────────

def translated_read(reference: str, languages) -> str:
    """How to read a translated column as text, on this site.

    The site's own language first and `en_US` behind it, because Odoo keeps the
    source term under `en_US` whatever language the record was entered in -- so
    this reads correctly on a record nobody has translated.
    """
    reads = [f"{reference}->>'{language}'" for language in languages]
    if len(reads) == 1:
        return reads[0]
    return f"COALESCE({', '.join(reads)})"


def company_read(reference: str, company_id) -> str:
    """How to read a per-company JSON column as text, for one company."""
    return f"{reference}->>'{int(company_id)}'"


class _Reader:
    """How each kind of JSON column is read as text on this site.

    A translated column is read through the site's languages; a per-company
    column through the connection's company. A per-company column with NO
    company to read it for is left exactly as written - the database's own
    error is better than a guess at whose figure it is.
    """

    def __init__(self, languages, company_id) -> None:
        self.languages, self.company_id = languages, company_id

    def handles(self, kind) -> bool:
        if kind == JSONB:
            return True
        return kind == JSONB_COMPANY and self.company_id is not None

    def read(self, kind, reference: str) -> str:
        if kind == JSONB_COMPANY:
            return company_read(reference, self.company_id)
        return translated_read(reference, self.languages)


def rewrite(sql: str, types_for_table, languages=("en_US",), company_id=None) -> str:
    """The same query, in the dialect this database actually speaks."""
    masked, literals = _mask(sql)
    schema = _Schema(masked, types_for_table)
    reader = _Reader(languages, company_id)

    masked = _fix_json_reads(masked, schema)
    masked = _fix_select_list(masked, schema, reader)
    masked = _fix_comparisons(masked, schema, reader)
    masked = _fix_sorting(masked, schema, reader)

    return _unmask(masked, literals)


def _reference(qualifier, column) -> str:
    return f"{qualifier}.{column}" if qualifier else column


def _fix_json_reads(sql: str, schema: _Schema) -> str:
    """`name->>'en_US'` on a column that is plain text -- drop the ->>.

    The agent learns from one failure that Odoo names are JSON and then applies
    it to the columns that are not, which fails the opposite way. Both halves
    of the oscillation are corrected, or it simply trades one error for the
    other.
    """
    def replace(match: re.Match) -> str:
        qualifier, column = match.group("qualifier"), match.group("column")
        kind = schema.type_of(qualifier, column)
        if kind is None or kind in JSON_KINDS:
            return match.group(0)
        return _reference(qualifier, column)

    return _JSON_READ.sub(replace, sql)


def _fix_comparisons(sql: str, schema: _Schema, reader: _Reader) -> str:
    def replace(match: re.Match) -> str:
        qualifier, column = match.group("qualifier"), match.group("column")
        kind = schema.type_of(qualifier, column)
        if not reader.handles(kind):
            return match.group(0)
        read = reader.read(kind, _reference(qualifier, column))
        return read + match.group(0)[match.end("column") - match.start():]

    for pattern in (_PATTERN_OPERATOR, _EQUALITY_TO_A_LITERAL, _IN_A_LIST_OF_LITERALS):
        sql = pattern.sub(replace, sql)
    return sql


def _top_level_span(sql: str):
    """Where the outermost SELECT list starts and ends.

    The SELECT it wants is the first one at parenthesis depth zero, which is
    NOT the first one in the string: `WITH recent AS (SELECT ...) SELECT ...`
    puts a whole query in front of it. Taking the first match outright found
    the CTE's SELECT, then ran into the CTE's closing paren and gave up -- so a
    query with a CTE quietly skipped this rewrite altogether and the agent got
    raw JSON back from a translated column.
    """
    depth_at = _depths(sql)
    opening = None
    for candidate in re.finditer(
        r"\bSELECT\b(\s+DISTINCT\b(\s+ON\s*\([^)]*\))?)?", sql, re.IGNORECASE
    ):
        if depth_at[candidate.start()] == 0:
            opening = candidate
            break
    if not opening:
        return None
    start = opening.end()
    depth = 0
    index = start
    while index < len(sql):
        character = sql[index]
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return None
            depth -= 1
        elif depth == 0 and re.match(r"\bFROM\b", sql[index:index + 4], re.IGNORECASE):
            if (index == 0 or not sql[index - 1].isalnum()) and \
               (index + 4 >= len(sql) or not (sql[index + 4].isalnum() or sql[index + 4] == "_")):
                return start, index
        index += 1
    return None


def _split_top_level(text: str) -> list[str]:
    items, depth, current = [], 0, []
    for character in text:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(character)
    items.append("".join(current))
    return items


def _fix_select_list(sql: str, schema: _Schema, reader: _Reader) -> str:
    """`SELECT name` on a translated column hands back `{"en_US": "Bank"}`.

    That one does not fail -- which is worse. The agent gets a JSON object
    where it asked for a name, and a customer reads it in the answer.
    """
    span = _top_level_span(sql)
    if not span:
        return sql
    start, end = span

    rewritten = []
    for item in _split_top_level(sql[start:end]):
        aliased = _ALIASED_ITEM.match(item)
        plain = _PLAIN_ITEM.match(item)
        if aliased:
            qualifier, column, alias = aliased.groups()
        elif plain:
            qualifier, column = plain.groups()
            alias = column
        else:
            rewritten.append(item)
            continue

        kind = schema.type_of(qualifier, column)
        if not reader.handles(kind):
            rewritten.append(item)
            continue

        read = reader.read(kind, _reference(qualifier, column))
        # THE ITEM'S OWN WHITESPACE IS KEPT, ON BOTH SIDES. The last item of a
        # SELECT list runs straight into FROM: `SELECT id, name FROM ...` is
        # split into "id" and " name ", and rewriting the second as
        # " name->>'en_US' AS name" glued the alias to the keyword -
        # `AS nameFROM account_account` - a syntax error on every query that
        # ended its list with a translated column. The test that covered
        # this asserted a substring and never read the whole statement.
        leading = item[:len(item) - len(item.lstrip())]
        trailing = item[len(item.rstrip()):]
        rewritten.append(f"{leading or ' '}{read} AS {alias}{trailing}")

    if rewritten and not rewritten[-1][-1:].isspace():
        rewritten[-1] += " "
    return sql[:start] + ",".join(rewritten) + sql[end:]


def _fix_sorting(sql: str, schema: _Schema, reader: _Reader) -> str:
    """ORDER BY / GROUP BY on a translated column sorts by raw JSON text.

    It runs, so nothing complains -- and the rows come back in an order that
    has nothing to do with the names the customer sees.
    """
    out = sql
    for clause in list(_SORT_CLAUSE.finditer(sql))[::-1]:
        start = clause.end()
        following = _CLAUSE_END.search(out, start)
        end = following.start() if following else len(out)

        items = []
        for item in _split_top_level(out[start:end]):
            direction = ""
            body = item
            tail = re.search(r"\s+(ASC|DESC)(\s+NULLS\s+(FIRST|LAST))?\s*$", item, re.IGNORECASE)
            if tail:
                body, direction = item[:tail.start()], item[tail.start():]

            plain = _PLAIN_ITEM.match(body)
            if not plain:
                items.append(item)
                continue
            qualifier, column = plain.groups()
            kind = schema.type_of(qualifier, column)
            if not reader.handles(kind):
                items.append(item)
                continue
            items.append(
                " " + reader.read(kind, _reference(qualifier, column)) + direction
            )

        out = out[:start] + ",".join(items) + out[end:]
    return out
