"""Postgres engine tests against a disposable database.

Run only when GOLDCHECK_TEST_PG_URL points at a throwaway Postgres database: the tests
create and drop the demo tables there. Example (local Docker):
  docker run -d --name goldcheck-pg-test -e POSTGRES_USER=goldcheck -e POSTGRES_PASSWORD=... \
      -e POSTGRES_DB=goldcheck -p 127.0.0.1:55432:5432 postgres:17.6
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

from goldcheck import blame, core, demo, shrink
from goldcheck.__main__ import main

URL = os.environ.get("GOLDCHECK_TEST_PG_URL")
TABLES = ["customers", "products", "orders", "order_items", "refunds"]


def _load_demo(url, folder):
    db, q, a = demo.build(folder)
    src = sqlite3.connect(db)
    with psycopg.connect(url, autocommit=True) as pg:
        for t in TABLES:
            pg.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        for stmt in demo.SCHEMA.strip().split(";"):
            if stmt.strip():
                pg.execute(stmt.replace("REAL", "DOUBLE PRECISION"))
        for t in TABLES:
            rows = src.execute(f"SELECT * FROM {t}").fetchall()
            if rows:
                pg.cursor().executemany(f"INSERT INTO {t} VALUES ({', '.join(['%s'] * len(rows[0]))})", rows)
    src.close()
    return q, a


@unittest.skipUnless(psycopg and URL, "set GOLDCHECK_TEST_PG_URL to a throwaway Postgres database")
class Postgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        q, a = _load_demo(URL, cls.dir.name)
        cls.questions = {x["id"]: x for x in core.load_questions(q)}
        cls.answers, _ = core.load_answers(a)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def _count(self, table="customers"):
        with psycopg.connect(URL) as c:
            return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_scores(self):
        # Same as SQLite except q10, whose strftime() does not exist in Postgres.
        con = core.connect_readonly(URL)
        try:
            results = core.score(con, list(self.questions.values()), self.answers)
        finally:
            con.close()
        s = core.summarise(results)
        self.assertEqual((s["passed"], s["silent_wrong"], s["loud_failures"]), (6, 4, 2))
        self.assertEqual({r.id: r.verdict for r in results}["q10"], "sql_error")

    def test_question_sql_cannot_change_anything(self):
        con = core.connect_readonly(URL)
        try:
            for sql in ["DELETE FROM customers", "SELECT 1; DELETE FROM customers",
                        "WITH d AS (DELETE FROM customers RETURNING *) SELECT count(*) FROM d",
                        "SET default_transaction_read_only = off", "CREATE TABLE x (a int)"]:
                _, rows, err = core.run_sql(con, sql)
                self.assertIsNone(rows, sql)
                self.assertTrue(err, sql)
            core.run_sql(con, "SELECT set_config('default_transaction_read_only', 'off', false)")
            self.assertTrue(core.run_sql(con, "WITH d AS (DELETE FROM customers RETURNING *) SELECT 1")[2])
            self.assertIn("timed out", core.run_sql(con, "SELECT pg_sleep(3)", timeout=0.3)[2])
        finally:
            con.close()
        self.assertEqual(self._count(), 60)

    def test_shrink_blame_write_and_source_untouched(self):
        before = {t: self._count(t) for t in TABLES}
        got = {}
        for qid in ("q02", "q03", "q04", "q08"):
            cx = shrink.shrink(URL, self.questions[qid], self.answers[qid])
            self.assertTrue(cx.one_minimal, qid)
            b = blame.explain(URL, self.questions[qid]["gold_sql"], self.answers[qid], cx)
            got[qid] = (cx.rows, b and b["clause"], b and b["full_db"])
        self.assertEqual(got, {"q02": (1, "WHERE", True), "q03": (4, "WHERE", True),
                               "q04": (2, "SELECT", True), "q08": (3, "FROM", True)})
        out = os.path.join(self.dir.name, "q08.sql")
        cx = shrink.shrink(URL, self.questions["q08"], self.answers["q08"])
        shrink.write(cx, self.questions["q08"], self.answers["q08"], out)
        self.assertTrue(cx.verified)
        self.assertEqual({t: self._count(t) for t in TABLES}, before)

    def test_schema_qualified_names_are_refused(self):
        q = {"id": "x", "question": "?", "gold_sql": "SELECT count(*) FROM public.orders"}
        with self.assertRaises(core.InputError):
            shrink.shrink(URL, q, "SELECT count(*) FROM public.orders WHERE status = 'x'")

    def test_cli_compare_export_and_no_secret_in_outputs(self):
        out, locked = os.path.join(self.dir.name, "c.html"), os.path.join(self.dir.name, "locked")
        code = main(["compare", "--db", URL, "--old", self.questions["q04"]["gold_sql"], "--new",
                     self.answers["q04"], "--out", out, "--export-tests", locked])
        self.assertEqual(code, 1)
        secret = psycopg.conninfo.conninfo_to_dict(URL).get("password")
        texts = [open(out, encoding="utf-8").read()]
        for root, _, files in os.walk(locked):
            texts += [open(os.path.join(root, f), encoding="utf-8").read() for f in files]
        self.assertTrue(secret and all(secret not in t for t in texts))
        with open(os.path.join(locked, "pair", "expected.json")) as fh:
            self.assertEqual(json.load(fh)["engine"], "postgres")
        env = dict(os.environ, GOLDCHECK_PG_URL=URL)
        run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", locked],
                             capture_output=True, text=True, env=env)
        self.assertIn("failures=1", run.stderr)


if __name__ == "__main__":
    unittest.main()
