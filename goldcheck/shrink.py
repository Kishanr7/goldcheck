"""Find a small database on which gold SQL and agent SQL still disagree.

The source database is only read. Every candidate is a fresh in-memory copy of the
schema with a subset of the rows, locked read-only before any question SQL runs.
A candidate is kept only if both queries still run and the comparison is still a
silent failure (wrong result, not an error). The search deletes chunks of rows
table by table (delta debugging) until no single row can be removed or the budget
runs out.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field

from .core import SILENT, InputError, compare, lock_readonly, run_sql

MAX_SOURCE_ROWS = 200_000


@dataclass
class Counterexample:
    question_id: str
    verdict: str
    detail: str
    tables: dict                     # name -> (columns, rows); only tables with rows are listed
    source_rows: int
    rows: int
    checks: int
    seconds: float
    one_minimal: bool                # no single remaining row can be deleted
    budget_exhausted: bool
    verified: bool = False           # re-checked on the written file
    gold: tuple = ((), ())           # (columns, rows) on the counterexample
    agent: tuple = ((), ())
    schema: list = field(default_factory=list)

    def to_sql(self) -> str:
        """A script that recreates the counterexample in any SQLite shell."""
        lines = [s.rstrip(";") + ";" for s in self.schema]
        for name, (cols, rows) in self.tables.items():
            col_list = ", ".join(_q(c) for c in cols)
            for r in rows:
                lines.append(f"INSERT INTO {_q(name)} ({col_list}) VALUES ({', '.join(_lit(v) for v in r)});")
        return "\n".join(lines) + "\n"


class _Budget(Exception):
    pass


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return f"X'{v.hex()}'"
    return "'" + str(v).replace("'", "''") + "'"


def load_source(path: str):
    """Return (schema statements, {table: (insertable columns, rows)}, has_foreign_keys)."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        objects = con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "AND type IN ('table', 'index', 'view') "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, rowid").fetchall()
    except sqlite3.Error as exc:
        raise InputError(f"Cannot open database read-only: {path} ({exc})") from exc
    try:
        schema, tables, has_fk, total = [], {}, False, 0
        for kind, name, sql in objects:
            schema.append(sql)
            if kind != "table" or sql.upper().startswith("CREATE VIRTUAL"):
                continue
            info = con.execute(f"PRAGMA table_xinfo({_q(name)})").fetchall()
            cols = [row[1] for row in info if row[6] == 0]  # skip generated/hidden columns
            rows = con.execute(f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(name)}").fetchall()
            total += len(rows)
            if total > MAX_SOURCE_ROWS:
                raise InputError(f"Database has more than {MAX_SOURCE_ROWS} rows; shrink a sample of it instead.")
            has_fk = has_fk or bool(con.execute(f"PRAGMA foreign_key_list({_q(name)})").fetchall())
            tables[name] = (cols, rows)
        return schema, tables, has_fk
    finally:
        con.close()


def _build(schema, tables, current, check_fk):
    con = sqlite3.connect(":memory:")
    for sql in schema:
        con.execute(sql)
    for name, rows in current.items():
        if rows:
            cols = tables[name][0]
            con.executemany(f"INSERT INTO {_q(name)} ({', '.join(_q(c) for c in cols)}) "
                            f"VALUES ({', '.join('?' * len(cols))})", rows)
    if check_fk and con.execute("PRAGMA foreign_key_check").fetchone():
        con.close()
        return None
    return lock_readonly(con)


def _observe(con, gold_sql, agent_sql, ordered, timeout):
    """Return (verdict, detail, gold, agent); verdict None when either query errors."""
    gc, gr, gerr = run_sql(con, gold_sql, timeout)
    ac, ar, aerr = run_sql(con, agent_sql, timeout)
    if gerr or aerr:
        return None, gerr or aerr, None, None
    verdict, detail = compare(gc, gr, ac, ar, ordered)
    return verdict, detail, (gc, gr), (ac, ar)


def shrink(db_path, question, agent_sql, timeout=5.0, max_checks=3000, max_seconds=60.0, source=None):
    """Return a Counterexample, or None if the two queries do not disagree silently on the full database."""
    schema, tables, has_fk = source or load_source(db_path)
    gold_sql, ordered = question["gold_sql"], bool(question.get("ordered"))
    start, checks = time.monotonic(), 0

    def fails(current):
        nonlocal checks
        if checks >= max_checks or time.monotonic() - start > max_seconds:
            raise _Budget
        checks += 1
        con = _build(schema, tables, current, has_fk)
        if con is None:
            return False
        try:
            return _observe(con, gold_sql, agent_sql, ordered, timeout)[0] in SILENT
        finally:
            con.close()

    current = {name: list(rows) for name, (_, rows) in tables.items()}
    source_rows = sum(len(r) for r in current.values())
    if not fails(current):
        return None

    budget_exhausted, one_minimal = False, False
    try:
        while True:
            changed = False
            for name in sorted(current, key=lambda n: -len(current[n])):
                rows = current[name]
                if rows and fails({**current, name: []}):
                    current[name], changed = [], True
                    continue
                n = 2
                while len(rows) >= 2:
                    size = -(-len(rows) // n)
                    for s in range(0, len(rows), size):
                        candidate = rows[:s] + rows[s + size:]
                        if fails({**current, name: candidate}):
                            rows, changed, n = candidate, True, max(2, n - 1)
                            break
                    else:
                        if n >= len(rows):
                            break
                        n = min(len(rows), n * 2)
                current[name] = rows
            if not changed:
                one_minimal = True
                break
    except _Budget:
        budget_exhausted = True

    con = _build(schema, tables, current, has_fk)
    verdict, detail, gold, agent = _observe(con, gold_sql, agent_sql, ordered, timeout)
    con.close()
    kept = {name: (tables[name][0], rows) for name, rows in current.items() if rows}
    return Counterexample(
        question_id=question["id"], verdict=verdict, detail=detail, tables=kept, source_rows=source_rows,
        rows=sum(len(r) for _, r in kept.values()), checks=checks, seconds=time.monotonic() - start,
        one_minimal=one_minimal, budget_exhausted=budget_exhausted, gold=gold, agent=agent, schema=schema)


def write(cx: Counterexample, question, agent_sql, out_path: str, timeout=5.0) -> None:
    """Write the counterexample to a new SQLite file and re-check it there."""
    if os.path.exists(out_path):
        raise InputError(f"Refusing to overwrite {out_path}; choose a new path.")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    con = sqlite3.connect(out_path)
    try:
        con.executescript(cx.to_sql())
        con.commit()
    finally:
        con.close()
    with sqlite3.connect(f"file:{out_path}?mode=ro", uri=True) as ro:
        lock_readonly(ro)
        verdict = _observe(ro, question["gold_sql"], agent_sql, bool(question.get("ordered")), timeout)[0]
    ro.close()
    cx.verified = verdict in SILENT
