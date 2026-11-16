"""Turn counterexamples into regression tests that run without goldcheck.

Each case folder holds the tiny database as SQL (fixture.sql), the query under test
(query.sql), the reference query (reference.sql, for humans) and the reference result on
the fixture (expected.json). test_regressions.py uses only the Python standard library:
it builds each fixture in memory, runs query.sql and compares with expected.json under the
same rules goldcheck uses. A case fails while the bug is present and passes once the query
returns the reference result on those rows.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

from .core import SILENT

TEST_FILE = r'''"""Regression tests exported by goldcheck. Standard library only.

Run:  python -m unittest discover -s THIS_FOLDER
Each case fails while query.sql still returns a different result from expected.json on
fixture.sql. Edit query.sql when you fix the query (or point it at your new SQL).
"""
import json
import math
import os
import sqlite3
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))


def _norm(v):
    if v is None:
        return ("null",)
    if isinstance(v, dict) and "hex" in v:
        return ("bytes", v["hex"])
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        return ("nan",) if math.isnan(f) else ("num", float(f"{f:.6g}"))
    if isinstance(v, bytes):
        return ("bytes", v.hex())
    return ("text", str(v).strip())


def _same(exp_rows, got_rows, ordered):
    """Same rows, ignoring column names/order and extra columns, numbers to 6 significant digits."""
    if len(exp_rows) != len(got_rows):
        return False
    if not exp_rows:
        return True
    n_exp, n_got = len(exp_rows[0]), len(got_rows[0])
    sig = lambda rows, j: tuple(sorted(repr(_norm(r[j])) for r in rows))
    got_sigs = [sig(got_rows, k) for k in range(n_got)]
    used, mapping = set(), []
    for j in range(n_exp):
        s = sig(exp_rows, j)
        hit = next((k for k in range(n_got) if k not in used and got_sigs[k] == s), None)
        if hit is None:
            return False
        used.add(hit)
        mapping.append(hit)
    e = [tuple(_norm(v) for v in r) for r in exp_rows]
    g = [tuple(_norm(r[k]) for k in mapping) for r in got_rows]
    return e == g if ordered else Counter(e) == Counter(g)


def _plain(v):
    import datetime
    import decimal
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.time, datetime.datetime, datetime.timedelta)):
        return str(v)
    return v


def _case(name):
    def test(self):
        folder = os.path.join(HERE, name)
        read = lambda f: open(os.path.join(folder, f), encoding="utf-8").read()
        expected = json.loads(read("expected.json"))
        if expected.get("engine") == "duckdb":
            import duckdb  # pip install duckdb
            con = duckdb.connect(":memory:")
            run_script = con.execute
        else:
            con = sqlite3.connect(":memory:")
            run_script = con.executescript
        try:
            run_script(read("fixture.sql"))
            got = [[_plain(v) for v in r] for r in con.execute(read("query.sql")).fetchall()]
        finally:
            con.close()
        self.assertTrue(_same(expected["rows"], got, expected["ordered"]),
                        f"{name}: expected {expected['rows'][:5]} but got {got[:5]}")
    return test


class Regressions(unittest.TestCase):
    pass


for _name in sorted(os.listdir(HERE)):
    if os.path.isfile(os.path.join(HERE, _name, "expected.json")):
        setattr(Regressions, "test_" + _name, _case(_name))

if __name__ == "__main__":
    unittest.main()
'''


def _still_differs(r) -> bool:
    """Rebuild the fixture from its SQL script and confirm the two queries still disagree there."""
    cx = r.counterexample
    if getattr(cx, "engine", "sqlite") == "duckdb":
        from . import duck
        con = duck.fixture(cx)
        try:
            return duck.observe(con, r.gold_sql, r.agent_sql, cx.ordered, 5.0)[0] in SILENT
        finally:
            con.close()
    from .shrink import _observe
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(cx.to_sql())
        return _observe(con, r.gold_sql, r.agent_sql, cx.ordered, 5.0)[0] in SILENT
    except sqlite3.Error:
        return False
    finally:
        con.close()


def _jsonable(v):
    import datetime
    import decimal
    if isinstance(v, bytes):
        return {"hex": v.hex()}
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.time, datetime.datetime, datetime.timedelta)):
        return str(v)
    return v


def _folder_name(qid: str, taken: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", qid).strip("_") or "case"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def export(results, out_dir: str) -> int:
    """Write one regression case per result that has a counterexample. Returns the count."""
    cases = [r for r in results if getattr(r, "counterexample", None) and _still_differs(r)]
    if os.path.exists(out_dir):
        raise FileExistsError(f"Refusing to overwrite {out_dir}; choose a new folder.")
    os.makedirs(out_dir)
    taken = set()
    for r in cases:
        cx = r.counterexample
        folder = os.path.join(out_dir, _folder_name(r.id, taken))
        os.makedirs(folder)
        files = {
            "fixture.sql": cx.to_sql(),
            "query.sql": r.agent_sql.strip() + "\n",
            "reference.sql": r.gold_sql.strip() + "\n",
            "expected.json": json.dumps({"id": r.id, "note": r.question, "ordered": bool(cx.ordered),
                                         "engine": getattr(cx, "engine", "sqlite"),
                                         "columns": list(cx.gold[0]),
                                         "rows": [[_jsonable(v) for v in row] for row in cx.gold[1]]},
                                        indent=1, ensure_ascii=False) + "\n",
        }
        for fname, text in files.items():
            with open(os.path.join(folder, fname), "w", encoding="utf-8") as fh:
                fh.write(text)
    with open(os.path.join(out_dir, "test_regressions.py"), "w", encoding="utf-8") as fh:
        fh.write(TEST_FILE)
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# goldcheck regression cases\n\n"
                 f"{len(cases)} case(s). Each folder is the smallest database goldcheck found on which "
                 "`query.sql` returns something different from `reference.sql`.\n\n"
                 "Run `python -m unittest discover -s .` in this folder (Python 3, nothing to install). "
                 "Each case fails while the difference is still there. When you fix the query, put the "
                 "new SQL in `query.sql`; the case passes once it returns `expected.json` on `fixture.sql`.\n\n"
                 "The fixtures contain real rows copied from your database. Review them before sharing.\n")
    return len(cases)
