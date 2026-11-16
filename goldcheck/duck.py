"""DuckDB engine: a .duckdb file, a Parquet/CSV/JSON file, or a folder of such files.

Optional: `pip install duckdb`. goldcheck itself stays standard-library only.

Safety model, mirroring the SQLite one: question SQL only ever runs on goldcheck's own
in-memory copy (or a read-only connection), after `enable_external_access = false` and
`lock_configuration = true`. That blocks file reads and writes, ATTACH, COPY and extension
installs, and the setting cannot be switched back. Only a single SELECT statement is
accepted, and each runs under a timeout.
"""
from __future__ import annotations

import datetime
import decimal
import os
import random
import threading
import time

from .core import MAX_ROWS, SILENT, InputError, compare

DB_SUFFIXES = {".duckdb", ".ddb"}
READERS = {".parquet": "read_parquet", ".csv": "read_csv_auto", ".tsv": "read_csv_auto",
           ".json": "read_json_auto", ".jsonl": "read_json_auto", ".ndjson": "read_json_auto"}
SAMPLE_SIZES = (1000, 100, 10)


def is_duck(path: str) -> bool:
    ext = os.path.splitext(str(path))[1].lower()
    return ext in DB_SUFFIXES or ext in READERS or os.path.isdir(str(path))


def _duckdb():
    try:
        import duckdb
    except ImportError:
        raise InputError("This input needs DuckDB: run `pip install duckdb`, then try again.") from None
    return duckdb


def _q(name) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sandbox(con):
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def _files(path):
    if os.path.isdir(path):
        found = [os.path.join(path, f) for f in sorted(os.listdir(path))
                 if os.path.splitext(f)[1].lower() in READERS]
        if not found:
            raise InputError(f"No .parquet, .csv or .json files in {path}")
        return found
    return [path]


def load(path, tables=None):
    """In-memory DuckDB holding the source (only `tables` when given), not yet sandboxed."""
    duckdb = _duckdb()
    if not os.path.exists(path):
        raise InputError(f"Not found: {path}")
    con = duckdb.connect(":memory:")
    try:
        if os.path.splitext(path)[1].lower() in DB_SUFFIXES:
            con.execute(f"ATTACH '{path.replace(chr(39), chr(39) * 2)}' AS src (READ_ONLY)")
            names = [r[0] for r in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src' AND schema_name = 'main'").fetchall()]
            for name in names:
                if tables is None or name.lower() in tables:
                    con.execute(f"CREATE TABLE {_q(name)} AS SELECT * FROM src.main.{_q(name)}")
            views = con.execute("SELECT sql FROM duckdb_views() WHERE database_name = 'src' AND schema_name = 'main' "
                                "AND NOT internal").fetchall()
            con.execute("DETACH src")
            for (sql,) in views:
                try:
                    con.execute(sql)
                except duckdb.Error:
                    pass  # a view over a table that was not copied
        else:
            for f in _files(path):
                name = os.path.splitext(os.path.basename(f))[0]
                if tables is None or name.lower() in tables:
                    reader = READERS[os.path.splitext(f)[1].lower()]
                    con.execute(f"CREATE TABLE {_q(name)} AS SELECT * FROM {reader}(?)", [f])
    except duckdb.Error as exc:
        con.close()
        raise InputError(f"Cannot load {path} with DuckDB ({exc})") from exc
    return con


def connect_readonly(path):
    return _sandbox(load(path))


def _plain(v):
    """DuckDB returns Decimal, dates and so on; compare them like SQLite values."""
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.time, datetime.datetime, datetime.timedelta)):
        return str(v)
    return v


def run_sql(con, sql, timeout: float = 5.0):
    """Return (columns, rows, error), like core.run_sql."""
    duckdb = _duckdb()
    if sql is None or not str(sql).strip():
        return None, None, "no_answer"
    try:
        statements = con.extract_statements(str(sql))
    except duckdb.Error as exc:
        return None, None, str(exc).splitlines()[0]
    if len(statements) != 1:
        return None, None, "only one SQL statement is allowed"
    if statements[0].type != duckdb.StatementType.SELECT:
        return None, None, "statement is not read-only (blocked)"
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        cur = con.execute(str(sql))
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS + 1)
        if len(rows) > MAX_ROWS:
            return None, None, f"more than {MAX_ROWS} rows returned"
        return cols, [tuple(_plain(v) for v in r) for r in rows], None
    except duckdb.InterruptException:
        return None, None, f"timed out after {timeout:g}s"
    except duckdb.PermissionException:
        return None, None, "statement needs file or network access (blocked)"
    except duckdb.Error as exc:
        return None, None, str(exc).splitlines()[0]
    finally:
        timer.cancel()


def observe(con, gold_sql, agent_sql, ordered, timeout):
    gc, gr, gerr = run_sql(con, gold_sql, timeout)
    ac, ar, aerr = run_sql(con, agent_sql, timeout)
    if gerr or aerr:
        return None, gerr or aerr, None, None
    verdict, detail = compare(gc, gr, ac, ar, ordered)
    return verdict, detail, (gc, gr), (ac, ar)


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, bytes):
        return "'" + "".join(f"\\x{b:02x}" for b in v) + "'::BLOB"
    return "'" + str(v).replace("'", "''") + "'"


def _literals(sqls):
    from .shrink import _literals as find
    return find(sqls)


def shrink(path, question, agent_sql, timeout=5.0, max_checks=3000, max_seconds=60.0):
    """DuckDB version of shrink.shrink. Same result type; no foreign keys to respect."""
    from .shrink import Counterexample, _Budget, _has_content
    duckdb = _duckdb()
    gold_sql, ordered = question["gold_sql"], bool(question.get("ordered"))
    start, checks = time.monotonic(), 0
    try:
        named = {n.lower() for n in duckdb.get_table_names(gold_sql) | duckdb.get_table_names(agent_sql)}
    except duckdb.Error:
        return None
    con = _sandbox(load(path, named))
    try:
        reads = [r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'memory'").fetchall()]
        keep = {t: [r[0] for r in con.execute(f"SELECT rowid FROM {_q(t)} ORDER BY rowid").fetchall()] for t in reads}
        informative = False

        def holds():
            nonlocal checks
            if checks >= max_checks or time.monotonic() - start > max_seconds:
                raise _Budget
            checks += 1
            verdict, _, gold, _ = observe(con, gold_sql, agent_sql, ordered, timeout)
            return verdict in SILENT and (not informative or _has_content(gold))

        def try_delete(deletions):
            con.execute("BEGIN TRANSACTION")
            try:
                for t, ids in deletions.items():
                    con.execute(f"DELETE FROM {_q(t)} WHERE rowid IN (SELECT unnest(?::BIGINT[]))", [list(ids)])
                ok = holds()
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT" if ok else "ROLLBACK")
            if ok:
                for t, ids in deletions.items():
                    gone = set(ids)
                    keep[t] = [r for r in keep[t] if r not in gone]
            return ok

        source_rows = sum(len(v) for v in keep.values())
        if not holds():
            return None
        informative = _has_content(observe(con, gold_sql, agent_sql, ordered, timeout)[2])
        budget_exhausted = one_minimal = False
        try:
            lits = sorted(_literals([gold_sql, agent_sql]))
            if lits:  # guess: rows equal to (or LIKE) a text literal of either query
                seed = {}
                for t in reads:
                    cols = [r[0] for r in con.execute(f"DESCRIBE {_q(t)}").fetchall() if r[1] == "VARCHAR"]
                    conds = [f"{_q(c)} {'LIKE' if '%' in lit or '_' in lit else '='} ?" for c in cols for lit in lits]
                    args = [lit for _c in cols for lit in lits]
                    if conds:
                        hits = {r[0] for r in con.execute(f"SELECT rowid FROM {_q(t)} WHERE {' OR '.join(conds)}",
                                                          args).fetchall()}
                        if hits:
                            seed[t] = hits
                proposal = {t: [r for r in keep[t] if r not in seed[t]] for t in seed}
                proposal = {t: ids for t, ids in proposal.items() if ids}
                if proposal:
                    try_delete(proposal)
            rng = random.Random(0)
            for t in sorted(keep, key=lambda n: -len(keep[n])):
                for size in SAMPLE_SIZES:
                    if len(keep[t]) > size * 2:
                        sample = set(rng.sample(keep[t], size))
                        try_delete({t: [r for r in keep[t] if r not in sample]})
            while True:
                changed = False
                for t in sorted(keep, key=lambda n: -len(keep[n])):
                    if keep[t] and try_delete({t: list(keep[t])}):
                        changed = True
                        continue
                    n = 2
                    while len(keep[t]) >= 2:
                        rows = keep[t]
                        size = -(-len(rows) // n)
                        for s in range(0, len(rows), size):
                            if try_delete({t: rows[s:s + size]}):
                                changed, n = True, max(2, n - 1)
                                break
                        else:
                            if n >= len(rows):
                                break
                            n = min(len(rows), n * 2)
                if not changed:
                    one_minimal = True
                    break
        except _Budget:
            budget_exhausted = True
        verdict, detail, gold, agent = observe(con, gold_sql, agent_sql, ordered, timeout)
        tables, schema = {}, []
        for t in sorted(keep):
            desc = con.execute(f"DESCRIBE {_q(t)}").fetchall()
            schema.append(f"CREATE TABLE {_q(t)} ({', '.join(f'{_q(c[0])} {c[1]}' for c in desc)})")
            if keep[t]:
                cols = [c[0] for c in desc]
                rows = con.execute(f"SELECT * FROM {_q(t)} ORDER BY rowid").fetchall()
                tables[t] = (cols, rows)
        cx = Counterexample(
            question_id=question["id"], verdict=verdict, detail=detail, tables=tables, source_rows=source_rows,
            rows=sum(len(v) for v in keep.values()), checks=checks, seconds=time.monotonic() - start,
            one_minimal=one_minimal, budget_exhausted=budget_exhausted, gold=gold, agent=agent, schema=schema,
            ordered=ordered)
        cx.engine = "duckdb"
        return cx
    finally:
        con.close()


def to_sql(cx) -> str:
    lines = [s.rstrip(";") + ";" for s in cx.schema]
    for name, (cols, rows) in cx.tables.items():
        col_list = ", ".join(_q(c) for c in cols)
        for r in rows:
            lines.append(f"INSERT INTO {_q(name)} ({col_list}) VALUES ({', '.join(_lit(v) for v in r)});")
    return "\n".join(lines) + "\n"


def fixture(cx):
    """Sandboxed in-memory DuckDB holding the counterexample."""
    duckdb = _duckdb()
    con = duckdb.connect(":memory:")
    con.execute(to_sql(cx))
    return _sandbox(con)
