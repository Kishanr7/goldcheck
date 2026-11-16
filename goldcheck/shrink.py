"""Find a small database on which gold SQL and agent SQL still disagree.

The source file is never opened for writing. goldcheck copies it to a private
temporary file, then deletes rows from the tables the two queries actually read
(found from SQLite's own query plan), in chunks of shrinking size (delta
debugging). A deletion is kept only if both queries still run and the comparison
is still a silent failure. Each deletion happens inside a SAVEPOINT and is rolled
back when it does not keep the failure. Question SQL only ever runs under the same
SELECT-only authorizer and timeout as normal scoring.

Declared foreign keys are respected: a deletion may not create a dangling
reference that did not already exist in the source.

The result keeps the full original rows, every table definition, and only the
kept rows of the tables the queries read.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field

from .core import SILENT, InputError, compare, lock_readonly, run_sql, unlock


SEED_LIMIT = 20_000      # largest per-table guess worth trying
SAMPLE_SIZES = (1000, 100, 10)  # random-sample guesses for large tables, largest first
TMP_INDEX = "goldcheck_tmp_"


@dataclass
class Counterexample:
    question_id: str
    verdict: str
    detail: str
    tables: dict                     # name -> (columns, rows); tables the queries read that kept rows
    source_rows: int                 # rows in the tables the queries read
    rows: int                        # kept rows in the tables the queries read
    checks: int
    seconds: float
    one_minimal: bool                # no single remaining row can be deleted
    budget_exhausted: bool
    verified: bool = False           # re-checked on the written file
    gold: tuple = ((), ())           # (columns, rows) on the counterexample
    agent: tuple = ((), ())
    ordered: bool = False            # row order is part of the comparison
    blame: object = None             # blame.explain() result: the clause that makes the difference
    context_rows: int = 0            # rows of unread tables included so references are not left dangling
    schema: list = field(default_factory=list)
    engine: str = "sqlite"

    def to_sql(self) -> str:
        """A script that recreates the counterexample in a SQLite (or DuckDB) shell."""
        if self.engine == "duckdb":
            from . import duck
            return duck.to_sql(self)
        if self.engine == "postgres":
            from . import pg
            return pg.to_sql(self)
        lines = [s.rstrip().rstrip(";") + ";" for s in self.schema]
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


def tables_read(con, sqls) -> set:
    """Tables in the main schema that any of `sqls` opens for reading, from EXPLAIN."""
    roots = {}
    for kind, name, tbl, root in con.execute(
            "SELECT type, name, tbl_name, rootpage FROM sqlite_master WHERE type IN ('table', 'index')"):
        if root:
            roots[root] = tbl
    found = set()
    for sql in sqls:
        for row in con.execute("EXPLAIN " + sql).fetchall():
            if row[1] == "OpenRead" and row[4] == 0 and row[3] in roots:
                found.add(roots[row[3]])
    return {t for t in found if not t.startswith("sqlite_")}


def _fk_list(con, child):
    """Declared foreign keys of `child` as [(parent, [child cols], [parent cols])]."""
    groups, out = {}, []
    for fid, _seq, parent, frm, to, *_ in con.execute(f"PRAGMA foreign_key_list({_q(child)})"):
        groups.setdefault(fid, [parent, [], []])
        groups[fid][1].append(frm)
        groups[fid][2].append(to)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for parent, frm, to in groups.values():
        if parent not in names:
            continue
        if any(t is None for t in to):
            pk = [r[1] for r in sorted(con.execute(f"PRAGMA table_info({_q(parent)})"), key=lambda r: r[5]) if r[5]]
            to = pk if len(pk) == len(frm) else to
        if not any(t is None for t in to):
            out.append((parent, frm, to))
    return out


def _foreign_keys(con, tables):
    """[(child, [child cols], parent, [parent cols])] where both ends are in `tables`."""
    # A parent outside `tables` is never reduced, so it cannot gain dangling references.
    return [(child, frm, parent, to) for child in tables
            for parent, frm, to in _fk_list(con, child) if parent in tables]


def _referenced_rows(con, keep, reads):
    """Rows of tables the queries do not read that kept rows point at, following chains."""
    extra = {}
    queue = [t for t in keep if keep[t]]
    while queue:
        child = queue.pop()
        ids = keep.get(child) or sorted(extra.get(child, ()))
        for parent, frm, to in _fk_list(con, child):
            if parent in reads:
                continue  # already consistent: deletions there never create new dangling references
            on = " AND ".join(f"p.{_q(t)} = c.{_q(f)}" for f, t in zip(frm, to))
            found = {r[0] for r in con.execute(
                f"SELECT p.rowid FROM json_each(?) j CROSS JOIN {_q(child)} c ON c.rowid = j.value "
                f"CROSS JOIN {_q(parent)} p ON {on}", (json.dumps(ids),))}
            new = found - extra.get(parent, set())
            if new:
                extra.setdefault(parent, set()).update(new)
                queue.append(parent)
    return extra


def _helper_indexes(con, reads):
    """Index both ends of every declared foreign key that touches a read table.

    Only in the private working copy, so reference checks stay linear on large tables.
    Query results do not depend on indexes; they are left out of the exported schema.
    """
    seen, n = set(), 0
    tables = set(reads)
    for child in list(tables) + [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
        for parent, frm, to in _fk_list(con, child):
            if child not in tables and parent not in tables:
                continue
            for table, cols in ((child, frm), (parent, to)):
                key = (table, tuple(cols))
                if key in seen:
                    continue
                seen.add(key)
                n += 1
                con.execute(f"CREATE INDEX {_q(TMP_INDEX + str(n))} ON {_q(table)} ({', '.join(_q(c) for c in cols)})")


def _dangling(con, fks):
    """Set of (fk index, child rowid) that point at a missing parent row."""
    bad = set()
    for i, (child, frm, parent, to) in enumerate(fks):
        on = " AND ".join(f"p.{_q(t)} = c.{_q(f)}" for f, t in zip(frm, to))
        notnull = " AND ".join(f"c.{_q(f)} IS NOT NULL" for f in frm)
        sql = (f"SELECT c.rowid FROM {_q(child)} c WHERE {notnull} "
               f"AND NOT EXISTS (SELECT 1 FROM {_q(parent)} p WHERE {on})")
        bad.update((i, r[0]) for r in con.execute(sql))
    return bad


def _cascade(con, fks, deletions):
    """Grow {table: rowids} with every row (in the reduced tables) that points at a deleted row.

    Deleting a question post then also deletes its answers, instead of rejecting the step
    because the answers would dangle. Uses the helper indexes, so the cost follows the
    deletion size.
    """
    out = {t: list(dict.fromkeys(ids)) for t, ids in deletions.items()}
    seen = {t: set(ids) for t, ids in out.items()}
    frontier = {t: list(ids) for t, ids in out.items()}
    while frontier:
        nxt = {}
        for child, frm, parent, to in fks:
            if not frontier.get(parent):
                continue
            on = " AND ".join(f"c.{_q(f)} = p.{_q(t)}" for f, t in zip(frm, to))
            for (rid,) in con.execute(
                    f"SELECT c.rowid FROM json_each(?) j CROSS JOIN {_q(parent)} p ON p.rowid = j.value "
                    f"CROSS JOIN {_q(child)} c ON {on}", (json.dumps(frontier[parent]),)):
                if rid not in seen.setdefault(child, set()):
                    seen[child].add(rid)
                    out.setdefault(child, []).append(rid)
                    nxt.setdefault(child, []).append(rid)
        frontier = nxt
    return out


def _deleted_keys(con, fks, deletions):
    """Before a deletion: {fk index: [key tuple, ...]} of parent rows about to go."""
    keys = {}
    for i, (_child, _frm, parent, to) in enumerate(fks):
        if parent in deletions:
            keys[i] = con.execute(
                f"SELECT {', '.join(_q(t) for t in to)} FROM {_q(parent)} "
                "WHERE rowid IN (SELECT value FROM json_each(?))", (json.dumps(deletions[parent]),)).fetchall()
    return keys


def _creates_dangling(con, fks, keys, allowed) -> bool:
    """After a deletion: does any child now point at a deleted key that no parent row still has?

    Only children of the deleted keys are looked at (through the helper indexes), so the
    cost follows the size of the deletion, not the size of the tables.
    """
    for i, rows in keys.items():
        child, frm, parent, to = fks[i]
        rows = [r for r in rows if all(v is not None for v in r)]
        if not rows:
            continue
        if any(isinstance(v, bytes) for r in rows for v in r):
            return bool(_dangling(con, [fks[i]]) - {(0, rid) for (j, rid) in allowed if j == i})
        on = " AND ".join(f"c.{_q(f)} = json_extract(j.value, '$[{k}]')" for k, f in enumerate(frm))
        still = " AND ".join(f"p.{_q(t)} = c.{_q(f)}" for f, t in zip(frm, to))
        for (rid,) in con.execute(
                f"SELECT c.rowid FROM json_each(?) j CROSS JOIN {_q(child)} c ON {on} "
                f"WHERE NOT EXISTS (SELECT 1 FROM {_q(parent)} p WHERE {still})", (json.dumps(rows),)):
            if (i, rid) not in allowed:
                return True
    return False


def _literals(sqls):
    """String literals in the SQL texts ('' is an escaped quote)."""
    found = set()
    for sql in sqls:
        i = 0
        while (i := sql.find("'", i)) != -1:
            j, buf = i + 1, []
            while j < len(sql):
                if sql[j] == "'" and sql[j + 1:j + 2] == "'":
                    buf.append("'")
                    j += 2
                elif sql[j] == "'":
                    break
                else:
                    buf.append(sql[j])
                    j += 1
            if buf:
                found.add("".join(buf))
            i = j + 1
    return found


def _parents_of(con, fk, ids):
    child, frm, parent, to = fk
    on = " AND ".join(f"p.{_q(b)} = c.{_q(a)}" for a, b in zip(frm, to))
    return {r[0] for r in con.execute(
        f"SELECT p.rowid FROM json_each(?) j CROSS JOIN {_q(child)} c ON c.rowid = j.value "
        f"CROSS JOIN {_q(parent)} p ON {on}", (json.dumps(sorted(ids)),))}


def _children_of(con, fk, ids):
    child, frm, parent, to = fk
    on = " AND ".join(f"p.{_q(b)} = c.{_q(a)}" for a, b in zip(frm, to))
    return {r[0] for r in con.execute(
        f"SELECT c.rowid FROM json_each(?) j CROSS JOIN {_q(parent)} p ON p.rowid = j.value "
        f"CROSS JOIN {_q(child)} c ON {on}", (json.dumps(sorted(ids)),))}


def _close_parents(con, fks, seed):
    """Add every row that seeded rows point at, until nothing new appears."""
    frontier = {t: set(v) for t, v in seed.items()}
    while frontier:
        nxt = {}
        for fk in fks:
            child, parent = fk[0], fk[2]
            if frontier.get(child):
                new = _parents_of(con, fk, frontier[child]) - seed.get(parent, set())
                if new:
                    seed.setdefault(parent, set()).update(new)
                    nxt.setdefault(parent, set()).update(new)
        if any(len(v) > SEED_LIMIT for v in seed.values()):
            return None
        frontier = nxt
    return seed


def _seed_candidates(con, reads, fks, sqls):
    """Guesses, smallest-effort first, each {table: set(rowids)}.

    Rows equal to (or LIKE) a string literal in either query, plus: (1) the rows that point
    at them and everything those point at; (2) only what the matched rows point at.
    Tables with no seeded row are left untouched by the guess. Only proposals: the caller
    verifies each one.
    """
    lits = sorted(_literals(sqls))
    if not lits:
        return []
    matched = {}
    for t in reads:
        cols = [r[1] for r in con.execute(f"PRAGMA table_xinfo({_q(t)})") if r[6] == 0]
        conds, args = [], []
        for c in cols:
            for lit in lits:
                conds.append(f"{_q(c)} LIKE ?" if "%" in lit or "_" in lit else f"{_q(c)} = ?")
                args.append(lit)
        hits = set()
        for k in range(0, len(conds), 500):  # stay under SQLite's variable limit
            hits.update(r[0] for r in con.execute(
                f"SELECT rowid FROM {_q(t)} WHERE {' OR '.join(conds[k:k + 500])}", args[k:k + 500]))
        if hits:
            matched[t] = hits
    if not matched or any(len(v) > SEED_LIMIT for v in matched.values()):
        return []
    out = []
    with_children = {t: set(v) for t, v in matched.items()}
    for fk in fks:
        if matched.get(fk[2]):
            with_children.setdefault(fk[0], set()).update(_children_of(con, fk, matched[fk[2]]))
    for guess in (with_children, {t: set(v) for t, v in matched.items()}):
        closed = _close_parents(con, fks, guess)
        if closed and closed not in out:
            out.append(closed)
    return out


def _has_content(result) -> bool:
    """True when a (columns, rows) result has at least one non-NULL value."""
    return bool(result) and any(v is not None for row in result[1] for v in row)


def _observe(con, gold_sql, agent_sql, ordered, timeout):
    """Return (verdict, detail, gold, agent); verdict None when either query errors."""
    lock_readonly(con)
    try:
        gc, gr, gerr = run_sql(con, gold_sql, timeout)
        ac, ar, aerr = run_sql(con, agent_sql, timeout)
    finally:
        unlock(con)
    if gerr or aerr:
        return None, gerr or aerr, None, None
    verdict, detail = compare(gc, gr, ac, ar, ordered)
    return verdict, detail, (gc, gr), (ac, ar)


def shrink(db_path, question, agent_sql, timeout=5.0, max_checks=3000, max_seconds=60.0, workdir=None):
    """Return a Counterexample, or None if the queries do not disagree silently on the database."""
    from . import duck, pg
    if pg.is_pg(db_path):
        return pg.shrink(db_path, question, agent_sql, timeout, max_checks, max_seconds)
    if duck.is_duck(db_path):
        return duck.shrink(db_path, question, agent_sql, timeout, max_checks, max_seconds)
    gold_sql, ordered = question["gold_sql"], bool(question.get("ordered"))
    start, checks = time.monotonic(), 0
    try:
        src = lock_readonly(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True))
        try:
            reads = tables_read(src, [gold_sql, agent_sql])
        finally:
            src.close()
    except (sqlite3.Error, sqlite3.Warning):
        return None  # a query that cannot even be planned cannot be a silent failure
    tmpdir = tempfile.mkdtemp(prefix="goldcheck-", dir=workdir)
    work = os.path.join(tmpdir, "work.sqlite")
    try:
        shutil.copyfile(db_path, work)
        con = sqlite3.connect(work, isolation_level=None)
        con.execute("PRAGMA journal_mode=MEMORY")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA foreign_keys=OFF")
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            con.execute(f"DROP TRIGGER {_q(name)}")
        try:
            keep = {t: [r[0] for r in con.execute(f"SELECT rowid FROM {_q(t)} ORDER BY rowid")] for t in reads}
        except sqlite3.OperationalError as exc:
            con.close()
            raise InputError(f"Cannot shrink this database ({exc}); WITHOUT ROWID tables are not supported.") from exc
        fks = _foreign_keys(con, reads)
        _helper_indexes(con, reads)
        allowed = _dangling(con, fks)

        informative = False  # set after the first check: must the expected result stay non-empty?

        def holds():
            nonlocal checks
            if checks >= max_checks or time.monotonic() - start > max_seconds:
                raise _Budget
            checks += 1
            verdict, _, gold, _ = _observe(con, gold_sql, agent_sql, ordered, timeout)
            if verdict not in SILENT:
                return False
            # A counterexample on which the expected result is empty (or all NULL) only says
            # "they differ on almost nothing". When the full database gives a real expected
            # result, keep one, so the rows show the difference on actual data.
            return not informative or _has_content(gold)

        def try_delete(deletions):
            """Delete {table: rowids}, plus rows that would be left pointing at them.

            Returns the full deletion when the failure still holds, else None (rolled back).
            """
            deletions = _cascade(con, fks, deletions)
            keys = _deleted_keys(con, fks, deletions)
            con.execute("SAVEPOINT cand")
            for table, rowids in deletions.items():
                con.execute(f"DELETE FROM {_q(table)} WHERE rowid IN (SELECT value FROM json_each(?))",
                            (json.dumps(rowids),))
            try:
                ok = not _creates_dangling(con, fks, keys, allowed) and holds()
            except BaseException:
                con.execute("ROLLBACK TO cand")
                con.execute("RELEASE cand")
                raise
            con.execute("RELEASE cand" if ok else "ROLLBACK TO cand")
            if not ok:
                con.execute("RELEASE cand")
                return None
            for table, rowids in deletions.items():
                gone = set(rowids)
                keep[table] = [r for r in keep[table] if r not in gone]
            return deletions

        source_rows = sum(len(v) for v in keep.values())
        if not holds():
            con.close()
            return None
        informative = _has_content(_observe(con, gold_sql, agent_sql, ordered, timeout)[2])

        budget_exhausted, one_minimal = False, False
        try:
            # One guided guess before blind halving: keep rows that match the queries' text
            # literals plus their foreign-key neighbours. Verified like any other step.
            for seed in _seed_candidates(con, reads, fks, [gold_sql, agent_sql]):
                proposal = {t: [r for r in keep[t] if r not in seed[t]] for t in seed if t in keep}
                proposal = {t: ids for t, ids in proposal.items() if ids}
                if proposal and try_delete(proposal):
                    break
            # Then, for each large table, guess that a small random sample still shows the
            # difference (true when it is spread over many rows, as with a wrong COUNT).
            rng = random.Random(0)
            for name in sorted(keep, key=lambda n: -len(keep[n])):
                for size in SAMPLE_SIZES:
                    if len(keep[name]) > size * 2:
                        sample = set(rng.sample(keep[name], size))
                        try_delete({name: [r for r in keep[name] if r not in sample]})
            # Delta debugging. A row is removed together with the rows that point at it, so
            # "minimal" means no single row (with its dependants) can be removed.
            while True:
                changed = False
                for name in sorted(keep, key=lambda n: -len(keep[n])):
                    if keep[name] and try_delete({name: list(keep[name])}):
                        changed = True
                        continue
                    n = 2
                    while len(keep[name]) >= 2:
                        rows = keep[name]
                        size = -(-len(rows) // n)
                        for s in range(0, len(rows), size):
                            if try_delete({name: rows[s:s + size]}):
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

        verdict, detail, gold, agent = _observe(con, gold_sql, agent_sql, ordered, timeout)
        extra = _referenced_rows(con, keep, reads)
        tables, context_rows = {}, 0
        for name in sorted(set(keep) | set(extra)):
            ids = keep.get(name) or sorted(extra.get(name, ()))
            if ids:
                cols = [r[1] for r in con.execute(f"PRAGMA table_xinfo({_q(name)})") if r[6] == 0]
                rows = con.execute(f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(name)} "
                                   "WHERE rowid IN (SELECT value FROM json_each(?)) ORDER BY rowid",
                                   (json.dumps(ids),)).fetchall()
                tables[name] = (cols, rows)
                if name not in keep:
                    context_rows += len(rows)
        schema = [r[0] for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            f"AND name NOT LIKE '{TMP_INDEX}%' AND type IN ('table', 'index', 'view') "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, rowid")]
        con.close()
        return Counterexample(
            question_id=question["id"], verdict=verdict, detail=detail, tables=tables, source_rows=source_rows,
            rows=sum(len(keep[t]) for t in keep), context_rows=context_rows, checks=checks, seconds=time.monotonic() - start,
            one_minimal=one_minimal, budget_exhausted=budget_exhausted, gold=gold, agent=agent, schema=schema,
            ordered=ordered)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def write(cx: Counterexample, question, agent_sql, out_path: str, timeout=5.0) -> None:
    """Write the counterexample to a new database file (SQLite, or DuckDB) and re-check it there."""
    if os.path.exists(out_path):
        raise InputError(f"Refusing to overwrite {out_path}; choose a new path.")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if cx.engine == "postgres":
        from . import pg
        if not out_path.lower().endswith(".sql"):
            raise InputError("For Postgres the counterexample is written as a SQL script: use an --out ending in .sql")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(cx.to_sql())
        con = pg.fixture(cx)
        try:
            verdict = pg.observe(con, question["gold_sql"], agent_sql, bool(question.get("ordered")), timeout)[0]
        finally:
            con.close()
        cx.verified = verdict in SILENT
        return
    if cx.engine == "duckdb":
        from . import duck
        duck._duckdb().connect(out_path).execute(cx.to_sql()).close()
        con = duck.connect_readonly(out_path)
        try:
            verdict = duck.observe(con, question["gold_sql"], agent_sql, bool(question.get("ordered")), timeout)[0]
        finally:
            con.close()
        cx.verified = verdict in SILENT
        return
    con = sqlite3.connect(out_path)
    try:
        con.executescript(cx.to_sql())
        con.commit()
    finally:
        con.close()
    ro = sqlite3.connect(f"file:{out_path}?mode=ro", uri=True)
    try:
        verdict = _observe(ro, question["gold_sql"], agent_sql, bool(question.get("ordered")), timeout)[0]
    finally:
        ro.close()
    cx.verified = verdict in SILENT
