"""PostgreSQL engine: `--db postgresql://user@host/dbname`. Optional: `pip install "psycopg[binary]"`.

Postgres is usually a live, shared database, so goldcheck never writes to it:

* Question SQL must start with SELECT, WITH, VALUES or TABLE, is sent with the extended
  protocol (the server accepts one statement only), runs in a READ ONLY transaction, inside a
  savepoint that is always rolled back, under a statement timeout.
* Shrinking copies only the tables the two queries read (found with EXPLAIN, which does not
  run them) into session-private TEMP tables. Postgres drops those when the session ends.
  Unqualified table names then resolve to the copies. If a query names a schema
  explicitly, goldcheck refuses to shrink rather than read the real table.
* The connection URL is never printed or written to reports or exported files. Prefer a
  URL without a password plus the PGPASSWORD environment variable, and a read-only role.
"""
from __future__ import annotations

import datetime
import decimal
import json
import random
import re
import time
import uuid

from .core import MAX_ROWS, SILENT, InputError, compare

MAX_COPY_ROWS = 2_000_000
SAMPLE_SIZES = (1000, 100, 10)
_FIRST = re.compile(r"^(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*(select|with|values|table)\b", re.I | re.S)


def is_pg(path) -> bool:
    return str(path).lower().startswith(("postgres://", "postgresql://"))


def redact(url: str) -> str:
    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", str(url))


def _psycopg():
    try:
        import psycopg
    except ImportError:
        raise InputError('Postgres needs psycopg: run `pip install "psycopg[binary]"`, then try again.') from None
    return psycopg


def _q(name) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def connect(url):
    psycopg = _psycopg()
    try:
        return psycopg.connect(url, autocommit=False, connect_timeout=10)
    except psycopg.Error as exc:
        raise InputError(f"Cannot connect to {redact(url)} ({str(exc).splitlines()[0]})") from None


def connect_readonly(url):
    con = connect(url)
    con.read_only = True  # every transaction starts as BEGIN READ ONLY
    return con


def _plain(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.time, datetime.datetime, datetime.timedelta, uuid.UUID)):
        return str(v)
    if isinstance(v, memoryview):
        return bytes(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, default=str)
    return v


def _exec(cur, sql, params=None):
    """Extended protocol even without parameters, so the server rejects multiple statements."""
    if params is None:
        return cur.execute(sql.replace("%", "%%"), [])
    return cur.execute(sql, params)


def run_sql(con, sql, timeout: float = 5.0):
    """Return (columns, rows, error), like core.run_sql. Never changes data."""
    psycopg = _psycopg()
    if sql is None or not str(sql).strip():
        return None, None, "no_answer"
    if not _FIRST.match(str(sql)):
        return None, None, "statement is not read-only (blocked)"
    outer = con.info.transaction_status != psycopg.pq.TransactionStatus.IDLE
    try:
        with con.transaction(force_rollback=True):  # a savepoint when already inside a transaction
            cur = con.cursor()
            cur.execute("SELECT set_config('statement_timeout', %s, true)", [str(max(1, int(timeout * 1000)))])
            _exec(cur, str(sql))
            if cur.description is None:
                return None, None, "statement returns no rows"
            cols = [d.name for d in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
            if len(rows) > MAX_ROWS:
                return None, None, f"more than {MAX_ROWS} rows returned"
            return cols, [tuple(_plain(v) for v in r) for r in rows], None
    except psycopg.errors.QueryCanceled:
        return None, None, f"timed out after {timeout:g}s"
    except psycopg.errors.ReadOnlySqlTransaction:
        return None, None, "statement is not read-only (blocked)"
    except psycopg.Error as exc:
        return None, None, str(exc).splitlines()[0]
    finally:
        if not outer and con.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            con.rollback()


def observe(con, gold_sql, agent_sql, ordered, timeout):
    gc, gr, gerr = run_sql(con, gold_sql, timeout)
    ac, ar, aerr = run_sql(con, agent_sql, timeout)
    if gerr or aerr:
        return None, gerr or aerr, None, None
    verdict, detail = compare(gc, gr, ac, ar, ordered)
    return verdict, detail, (gc, gr), (ac, ar)


def tables_read(con, sqls):
    """{(schema, table)} that any of `sqls` reads, from EXPLAIN (VERBOSE, FORMAT JSON)."""
    psycopg = _psycopg()
    found = set()

    def walk(node):
        if isinstance(node, dict):
            if "Relation Name" in node:
                found.add((node.get("Schema"), node["Relation Name"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for sql in sqls:
        if not _FIRST.match(str(sql)):
            raise InputError("only SELECT-style queries can be shrunk")
        try:
            with con.transaction(force_rollback=True):
                cur = con.cursor()
                _exec(cur, "EXPLAIN (VERBOSE, FORMAT JSON) " + str(sql))
                walk(cur.fetchone()[0])
        except psycopg.Error as exc:
            raise InputError(f"cannot plan query ({str(exc).splitlines()[0]})") from None
    return found


def _foreign_keys(con, tables):
    """[(child, [cols], parent, [cols])] between the given (schema, table) pairs, by table name."""
    rows = con.execute("""
        SELECT cn.nspname, c.relname, pn.nspname, p.relname,
               ARRAY(SELECT attname FROM unnest(k.conkey) WITH ORDINALITY u(n, i)
                     JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.n ORDER BY u.i),
               ARRAY(SELECT attname FROM unnest(k.confkey) WITH ORDINALITY u(n, i)
                     JOIN pg_attribute a ON a.attrelid = k.confrelid AND a.attnum = u.n ORDER BY u.i)
        FROM pg_constraint k
        JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace cn ON cn.oid = c.relnamespace
        JOIN pg_class p ON p.oid = k.confrelid JOIN pg_namespace pn ON pn.oid = p.relnamespace
        WHERE k.contype = 'f'""").fetchall()
    return [(c, list(fc), p, list(pc)) for cs, c, ps, p, fc, pc in rows
            if (cs, c) in tables and (ps, p) in tables]


def _cascade(con, fks, deletions):
    out = {t: list(dict.fromkeys(ids)) for t, ids in deletions.items()}
    seen = {t: set(ids) for t, ids in out.items()}
    frontier = {t: list(ids) for t, ids in out.items()}
    while frontier:
        nxt = {}
        for child, frm, parent, to in fks:
            if not frontier.get(parent):
                continue
            on = " AND ".join(f"c.{_q(f)} = p.{_q(t)}" for f, t in zip(frm, to))
            for (tid,) in con.execute(
                    f"SELECT c.ctid::text FROM pg_temp.{_q(parent)} p JOIN pg_temp.{_q(child)} c ON {on} "
                    "WHERE p.ctid = ANY(%s::tid[])", [frontier[parent]]).fetchall():
                if tid not in seen.setdefault(child, set()):
                    seen[child].add(tid)
                    out.setdefault(child, []).append(tid)
                    nxt.setdefault(child, []).append(tid)
        frontier = nxt
    return out


def shrink(url, question, agent_sql, timeout=5.0, max_checks=3000, max_seconds=60.0):
    """Postgres version of shrink.shrink, working on session-private TEMP copies."""
    from .shrink import Counterexample, _Budget, _has_content, _literals
    psycopg = _psycopg()
    gold_sql, ordered = question["gold_sql"], bool(question.get("ordered"))
    start, checks = time.monotonic(), 0
    con = connect(url)
    try:
        con.read_only = True
        reads = tables_read(con, [gold_sql, agent_sql])
        con.rollback()
        names = [t for _, t in reads]
        if len(set(names)) != len(names):
            raise InputError("two tables with the same name in different schemas; cannot shrink")
        con.read_only = False
        total = 0
        for schema, table in sorted(reads):
            total += con.execute(f"SELECT count(*) FROM {_q(schema)}.{_q(table)}").fetchone()[0]
            if total > MAX_COPY_ROWS:
                raise InputError(f"the queried tables hold more than {MAX_COPY_ROWS} rows; shrink a sample")
            con.execute(f"CREATE TEMP TABLE {_q(table)} AS SELECT * FROM {_q(schema)}.{_q(table)}")
        fks = _foreign_keys(con, reads)
        for n, (child, frm, parent, to) in enumerate(fks):
            con.execute(f"CREATE INDEX ON pg_temp.{_q(child)} ({', '.join(_q(c) for c in frm)})")
            con.execute(f"CREATE INDEX ON pg_temp.{_q(parent)} ({', '.join(_q(c) for c in to)})")
        con.commit()
        con.read_only = True  # deletes on TEMP tables are still allowed
        if any(not (s or "").startswith("pg_temp") for s, _ in tables_read(con, [gold_sql, agent_sql])):
            raise InputError("a query names a schema explicitly (e.g. public.orders); "
                             "use unqualified table names to shrink")
        con.rollback()
        keep = {t: [r[0] for r in con.execute(f"SELECT ctid::text FROM pg_temp.{_q(t)} ORDER BY ctid").fetchall()]
                for t in names}
        con.rollback()
        informative = False

        def holds():
            nonlocal checks
            if checks >= max_checks or time.monotonic() - start > max_seconds:
                raise _Budget
            checks += 1
            verdict, _, gold, _ = observe(con, gold_sql, agent_sql, ordered, timeout)
            return verdict in SILENT and (not informative or _has_content(gold))

        def try_delete(deletions):
            try:
                deletions = _cascade(con, fks, deletions)
                for t, ids in deletions.items():
                    con.execute(f"DELETE FROM pg_temp.{_q(t)} WHERE ctid = ANY(%s::tid[])", [ids])
                ok = holds()
            except BaseException:
                con.rollback()
                raise
            if not ok:
                con.rollback()
                return None
            con.commit()
            for t, ids in deletions.items():
                gone = set(ids)
                keep[t] = [r for r in keep[t] if r not in gone]
            return deletions

        source_rows = sum(len(v) for v in keep.values())
        if not holds():
            return None
        informative = _has_content(observe(con, gold_sql, agent_sql, ordered, timeout)[2])
        con.rollback()
        budget_exhausted = one_minimal = False
        try:
            lits = sorted(_literals([gold_sql, agent_sql]))
            if lits:
                seed = {}
                for t in names:
                    cols = [r[0] for r in con.execute(
                        "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 "
                        "AND NOT a.attisdropped AND a.atttypid IN ('text'::regtype, 'varchar'::regtype, 'bpchar'::regtype)",
                        [f"pg_temp.{_q(t)}"]).fetchall()]
                    conds = [f"{_q(c)} {'LIKE' if '%' in lit or '_' in lit else '='} %s" for c in cols for lit in lits]
                    if conds:
                        hits = {r[0] for r in con.execute(
                            f"SELECT ctid::text FROM pg_temp.{_q(t)} WHERE {' OR '.join(conds)}",
                            [lit for _c in cols for lit in lits]).fetchall()}
                        if hits:
                            seed[t] = hits
                con.rollback()
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
            cols = con.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
                [f"pg_temp.{_q(t)}"]).fetchall()
            schema.append(f"CREATE TABLE {_q(t)} ({', '.join(f'{_q(c)} {ty}' for c, ty in cols)})")
            if keep[t]:
                rows = con.execute(f"SELECT * FROM pg_temp.{_q(t)} ORDER BY ctid").fetchall()
                tables[t] = ([c for c, _ in cols], rows)
        con.rollback()
        cx = Counterexample(
            question_id=question["id"], verdict=verdict, detail=detail, tables=tables, source_rows=source_rows,
            rows=sum(len(v) for v in keep.values()), checks=checks, seconds=time.monotonic() - start,
            one_minimal=one_minimal, budget_exhausted=budget_exhausted, gold=gold, agent=agent, schema=schema,
            ordered=ordered, engine="postgres")
        cx.source = url  # kept in memory only, to rebuild the fixture on the same server
        return cx
    except psycopg.Error as exc:
        raise InputError(f"Postgres error while shrinking ({str(exc).splitlines()[0]})") from None
    finally:
        con.close()  # drops the TEMP copies


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, decimal.Decimal)):
        return str(v)
    if isinstance(v, (bytes, memoryview)):
        return "'\\x" + bytes(v).hex() + "'"
    if isinstance(v, (dict, list)):
        v = json.dumps(v, default=str)
    return "'" + str(v).replace("'", "''") + "'"


def to_sql(cx) -> str:
    lines = [s.rstrip(";") + ";" for s in cx.schema]
    for name, (cols, rows) in cx.tables.items():
        col_list = ", ".join(_q(c) for c in cols)
        for r in rows:
            lines.append(f"INSERT INTO {_q(name)} ({col_list}) VALUES ({', '.join(_lit(v) for v in r)});")
    return "\n".join(lines) + "\n"


def fixture(cx):
    """A read-only session on the same server where the counterexample exists as TEMP tables."""
    con = connect(cx.source)
    cur = con.cursor()
    for stmt in cx.schema:
        _exec(cur, stmt.replace("CREATE TABLE ", "CREATE TEMP TABLE ", 1))
    for name, (cols, rows) in cx.tables.items():
        cur.executemany(f"INSERT INTO pg_temp.{_q(name)} ({', '.join(_q(c) for c in cols)}) "
                        f"VALUES ({', '.join(['%s'] * len(cols))})", rows)
    con.commit()
    con.read_only = True
    return con
