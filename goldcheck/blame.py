"""Point at the clause that makes two queries disagree.

Both queries are split into their top-level clauses (SELECT, FROM with its joins, WHERE,
GROUP BY, HAVING, ORDER BY, LIMIT). For each clause, the checked query gets the expected
query's version of that clause, and the patched query runs on the counterexample database.
If the results then match, that clause is reported, and the patch is re-run on the full
database to say whether it explains the difference there too. Everything is executed, not
guessed; queries with WITH or set operations at the top level are not split.
"""
from __future__ import annotations

import re
import sqlite3

from .core import compare, lock_readonly, run_sql

CLAUSES = ["SELECT", "FROM", "WHERE", "GROUP BY", "HAVING", "ORDER BY", "LIMIT"]
TRY_ORDER = ["WHERE", "FROM", "SELECT", "GROUP BY", "HAVING", "ORDER BY", "LIMIT"]
_KEYWORD = re.compile(r"(SELECT|FROM|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|UNION|INTERSECT|EXCEPT|WITH|WINDOW)\b",
                      re.IGNORECASE)


def split(sql: str):
    """{clause: text without its keyword} for a plain top-level SELECT, else None."""
    sql = sql.strip().rstrip(";").strip()
    marks, depth, i, quote = [], 0, 0, None
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "[":
            quote = "]"
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            m = _KEYWORD.match(sql, i)
            if m:
                word = re.sub(r"\s+", " ", m.group(1).upper())
                if word in ("UNION", "INTERSECT", "EXCEPT", "WITH", "WINDOW"):
                    return None
                marks.append((word, i, m.end()))
                i = m.end()
                continue
        i += 1
    if not marks or marks[0][0] != "SELECT" or marks[0][1] != 0:
        return None
    parts = {}
    for n, (word, _start, body) in enumerate(marks):
        end = marks[n + 1][1] if n + 1 < len(marks) else len(sql)
        if word in parts:
            return None
        parts[word] = sql[body:end].strip()
    return parts


def join(parts) -> str:
    return " ".join(f"{k} {parts[k]}" for k in CLAUSES if parts.get(k) is not None)


def _matches(con, expected_sql, sql, ordered, timeout):
    gc, gr, gerr = run_sql(con, expected_sql, timeout)
    ac, ar, aerr = run_sql(con, sql, timeout)
    return not gerr and not aerr and compare(gc, gr, ac, ar, ordered)[0] == "pass"


def explain(db_path, expected_sql, actual_sql, cx, ordered=False, timeout=5.0):
    """Return {"clause", "expected", "actual", "full_db"}, or None if no one or two clauses explain it."""
    old, new = split(expected_sql), split(actual_sql)
    if not old or not new:
        return None
    fixture = sqlite3.connect(":memory:")
    try:
        fixture.executescript(cx.to_sql())
        lock_readonly(fixture)
        differing = [c for c in TRY_ORDER if old.get(c) != new.get(c)]
        # One clause first; then two together, because a wrong filter often comes with a wrong join.
        groups = [(c,) for c in differing] + [(a, b) for i, a in enumerate(differing) for b in differing[i + 1:]]
        for group in groups:
            patched = join(dict(new, **{c: old.get(c) for c in group}))
            if _matches(fixture, expected_sql, patched, ordered, timeout):
                clause = " + ".join(group)
                full = None
                try:
                    con = lock_readonly(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True))
                    try:
                        full = _matches(con, expected_sql, patched, ordered, timeout)
                    finally:
                        con.close()
                except sqlite3.Error:
                    pass
                def show(parts):
                    lines = [f"{c} {parts[c]}" if len(group) > 1 else parts[c]
                             for c in group if parts.get(c) is not None]
                    return "\n".join(lines) or None
                return {"clause": clause, "expected": show(old), "actual": show(new),
                        "full_db": full, "patched_sql": patched}
    except sqlite3.Error:
        return None
    finally:
        fixture.close()
    return None
