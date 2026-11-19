"""Look at the data before trusting a query: describe_values and check_sql.

Many wrong answers from text-to-SQL models are not bad SQL (42% in a BIRD sample). The model compares a column
with a value the data never contains: `bond_type = 'single'` when the column holds '-',
`currency = 'Euro'` when it holds 'EUR', a date written '2019/8/20' when rows say
'2019-08-20'. These checks need no gold answer:

* describe_values(con, table, column) shows the values a column really holds, so an
  agent can look before it writes.
* check_sql(con, sql) runs the query, finds every comparison of a column with a text
  literal (=, !=, IN, NOT IN, LIKE), and reports literals that match no row, with the
  closest real values as suggestions. It also warns when the result is empty.

All SQL goes through core.run_sql, so it is read-only, single-statement and time-limited
on every engine (SQLite, DuckDB, Postgres).
"""
from __future__ import annotations

import difflib
import re
import sqlite3

from .core import InputError, run_sql

VALUE_LIMIT = 5000          # distinct values fetched when looking for suggestions
SMALL_DOMAIN = 30           # list every value when a column has at most this many

_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)'
_STR = r"'(?:[^']|'')*'"
_COMPARE = re.compile(
    rf"(?:({_IDENT})\s*\.\s*)?({_IDENT})\s*(=|==|!=|<>|\bNOT\s+LIKE\b|\bLIKE\b)\s*({_STR})", re.I)
_IN_LIST = re.compile(
    rf"(?:({_IDENT})\s*\.\s*)?({_IDENT})\s+(\bNOT\s+IN\b|\bIN\b)\s*\(\s*({_STR}(?:\s*,\s*{_STR})*)\s*\)", re.I)
_FROM = re.compile(rf"\b(?:FROM|JOIN)\s+(?:({_IDENT})\s*\.\s*)?({_IDENT})(?:\s+(?:AS\s+)?({_IDENT}))?", re.I)
_KEYWORDS = {"where", "on", "join", "inner", "left", "right", "full", "cross", "group", "order", "limit",
             "having", "union", "natural", "using", "outer", "lateral", "window", "select", "as"}


def _unquote(name):
    if name and name[0] in '"`[':
        return name[1:-1]
    return name


def _lit(text):
    return text[1:-1].replace("''", "'")


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


def _sql_str(value):
    return "'" + str(value).replace("'", "''") + "'"


def _engine(con):
    if isinstance(con, sqlite3.Connection):
        return "sqlite"
    return "postgres" if type(con).__module__.startswith("psycopg") else "duckdb"


def list_tables(con):
    """Base tables, sorted. Raises InputError if they cannot be listed."""
    eng = _engine(con)
    if eng == "sqlite":
        sql = "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    elif eng == "duckdb":
        sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
    else:
        sql = ("SELECT table_name FROM information_schema.tables "
               "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
               "AND table_schema NOT LIKE 'pg_temp%' ORDER BY table_name")
    _, rows, err = run_sql(con, sql)
    if err:
        raise InputError(f"cannot list tables ({err})")
    return [r[0] for r in rows]


def columns(con, table):
    cols, _, err = run_sql(con, f"SELECT * FROM {_q(table)} LIMIT 0")
    if err:
        raise InputError(f"cannot read table {table} ({err})")
    return cols


def _resolve(name, options, kind):
    exact = [o for o in options if o == name]
    if exact:
        return exact[0]
    folded = [o for o in options if o.lower() == name.lower()]
    if len(folded) == 1:
        return folded[0]
    near = difflib.get_close_matches(name, options, n=3, cutoff=0.6)
    hint = f" Did you mean {', '.join(near)}?" if near else ""
    raise InputError(f"no {kind} named {name}.{hint}")


def describe_values(con, table=None, column=None, limit=20):
    """Without a table: list tables. With a table: its columns and sample values.
    With a column: its most common values, distinct count, NULL count, and min/max."""
    tables = list_tables(con)
    if not table:
        return {"tables": [{"table": t, "columns": columns(con, t)} for t in tables]}
    table = _resolve(table, tables, "table")
    cols = columns(con, table)
    if not column:
        out = []
        for c in cols:
            _, rows, err = run_sql(con, f"SELECT {_q(c)}, COUNT(*) FROM {_q(table)} WHERE {_q(c)} IS NOT NULL "
                                        f"GROUP BY {_q(c)} ORDER BY COUNT(*) DESC LIMIT 5")
            out.append({"column": c, "sample_values": [r[0] for r in rows] if not err else [], "error": err})
        return {"table": table, "columns": out}
    column = _resolve(column, cols, "column")
    c, t = _q(column), _q(table)
    _, stats, err = run_sql(con, f"SELECT COUNT(*), COUNT({c}), COUNT(DISTINCT {c}), MIN({c}), MAX({c}) FROM {t}")
    if err:
        raise InputError(f"cannot describe {table}.{column} ({err})")
    total, non_null, distinct, lo, hi = stats[0]
    _, top, err = run_sql(con, f"SELECT {c}, COUNT(*) FROM {t} WHERE {c} IS NOT NULL "
                               f"GROUP BY {c} ORDER BY COUNT(*) DESC, {c} LIMIT {int(limit)}")
    return {"table": table, "column": column, "rows": total, "nulls": total - non_null,
            "distinct": distinct, "min": lo, "max": hi,
            "top_values": [{"value": v, "count": n} for v, n in (top or [])],
            "all_values_shown": distinct <= int(limit)}


def _aliases(sql, tables):
    """{alias or table name (lower): real table} for tables named in FROM/JOIN clauses."""
    found = {}
    for _schema, name, alias in _FROM.findall(sql):
        name = _unquote(name)
        real = next((t for t in tables if t.lower() == name.lower()), None)
        if real is None:
            continue
        found[real.lower()] = real
        if alias and _unquote(alias).lower() not in _KEYWORDS:
            found[_unquote(alias).lower()] = real
    return found


def _comparisons(sql):
    """(qualifier, column, operator, [literals]) for each column-vs-text-literal comparison."""
    out = []
    for qual, col, op, lit in _COMPARE.findall(sql):
        out.append((_unquote(qual), _unquote(col), re.sub(r"\s+", " ", op.upper()), [_lit(lit)]))
    for qual, col, op, lits in _IN_LIST.findall(sql):
        out.append((_unquote(qual), _unquote(col), re.sub(r"\s+", " ", op.upper()),
                    [_lit(m) for m in re.findall(_STR, lits)]))
    return out


def _suggest(literal, values):
    """Closest real values: same text ignoring case/spacing first, then fuzzy matches."""
    text = [str(v) for v in values if v is not None]
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    same = [v for v in text if norm(v) == norm(literal) and norm(literal)]
    contains = [v for v in text if norm(literal) and (norm(literal) in norm(v) or (norm(v) and norm(v) in norm(literal)))]
    fuzzy = difflib.get_close_matches(literal, text, n=5, cutoff=0.5)
    fuzzy += difflib.get_close_matches(literal.lower(), [v.lower() for v in text], n=5, cutoff=0.5)
    seen, picks = set(), []
    for v in same + contains[:5] + fuzzy:
        match = next((t for t in text if t.lower() == v.lower()), v)
        if match not in seen:
            seen.add(match)
            picks.append(match)
    return picks[:5]


def check_sql(con, sql, timeout=5.0):
    """Return {"ok", "findings", "columns", "rows"}. ok is False when any finding is an error."""
    findings = []
    cols, rows, err = run_sql(con, sql, timeout)
    if err:
        return {"ok": False, "findings": [{"severity": "error", "kind": "sql_error", "message": err}],
                "columns": None, "rows": None}
    try:
        tables = list_tables(con)
    except InputError:
        tables = []
    aliases = _aliases(sql, tables)
    in_query = sorted(set(aliases.values()))
    col_cache = {}
    checked = []
    for qual, col, op, literals in _comparisons(sql):
        if qual:
            cands = [aliases[qual.lower()]] if qual.lower() in aliases else []
        else:
            cands = in_query
        hits = []
        for t in cands:
            if t not in col_cache:
                try:
                    col_cache[t] = columns(con, t)
                except InputError:
                    col_cache[t] = []
            real = next((c for c in col_cache[t] if c.lower() == col.lower()), None)
            if real:
                hits.append((t, real))
        if len(hits) != 1:
            continue  # unknown or ambiguous column: say nothing rather than guess
        table, column = hits[0]
        like = "LIKE" in op
        negated = op in ("!=", "<>", "NOT LIKE", "NOT IN")
        for literal in literals:
            pred = f"{_q(column)} {'LIKE' if like else '='} {_sql_str(literal)}"
            _, count, cerr = run_sql(con, f"SELECT COUNT(*) FROM {_q(table)} WHERE {pred}", timeout)
            if cerr:
                continue
            checked.append((table, column, op, literal, negated, count[0][0] > 0, op in ("IN", "NOT IN")))
    has_or = re.search(r"\bOR\b", re.sub(_STR, "''", sql), re.I) is not None
    matched = {(t, c) for t, c, _op, _lit_, neg, hit, _in in checked if hit and not neg}
    for table, column, op, literal, negated, hit, from_in in checked:
        if hit:
            continue
        _, vals, verr = run_sql(con, f"SELECT DISTINCT {_q(column)} FROM {_q(table)} "
                                     f"WHERE {_q(column)} IS NOT NULL LIMIT {VALUE_LIMIT}", timeout)
        vals = [v[0] for v in (vals or [])]
        suggestions = _suggest(literal.strip("%"), vals) if not verr else []
        # A value that is one of several alternatives (OR / IN) while another alternative for the
        # same column does match, or a defensive empty string, does not make the query wrong.
        alternative = not negated and ((table, column) in matched and (has_or or from_in) or literal == "")
        shown = literal if len(literal) <= 80 else literal[:77] + "..."
        if negated:
            tail = ", so this condition removes nothing"
        elif alternative:
            tail = "; it looks like an unused alternative, since the other values of this condition do match"
            if literal == "":
                tail = "; an empty-string check like this is often defensive"
        else:
            tail = ", so this condition matches nothing"
        finding = {
            "severity": "warning" if negated or alternative else "error",
            "kind": "filter_excludes_nothing" if negated else "value_not_found",
            "table": table, "column": column, "operator": op, "literal": literal,
            "message": f"{table}.{column} {op} '{shown}': no row has this value{tail}",
            "suggestions": suggestions,
        }
        if len(vals) <= SMALL_DOMAIN:
            finding["all_values"] = sorted(map(str, vals))
        findings.append(finding)
    if not rows or all(v is None for r in rows for v in r):
        findings.append({"severity": "warning", "kind": "empty_result",
                         "message": "the query returns no rows" if not rows else "the query returns only NULL"})
    return {"ok": not any(f["severity"] == "error" for f in findings), "findings": findings,
            "columns": cols, "rows": rows[:10] if rows else rows}
