"""DuckDB engine tests. Skipped when the optional `duckdb` package is not installed."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None

from goldcheck import blame, core, demo, shrink
from goldcheck.__main__ import main

TABLES = ["customers", "products", "orders", "order_items", "refunds"]


def _demo_duckdb(folder):
    """The demo shop as a .duckdb file and as a folder of Parquet files."""
    db, q, a = demo.build(folder)
    src = sqlite3.connect(db)
    ddb_path = os.path.join(folder, "shop.duckdb")
    ddb = duckdb.connect(ddb_path)
    for stmt in demo.SCHEMA.strip().split(";"):
        if stmt.strip():
            ddb.execute(stmt.replace("REAL", "DOUBLE"))
    for t in TABLES:
        rows = src.execute(f"SELECT * FROM {t}").fetchall()
        if rows:
            ddb.executemany(f"INSERT INTO {t} VALUES ({', '.join('?' * len(rows[0]))})", rows)
    parquet = os.path.join(folder, "parquet")
    os.makedirs(parquet)
    for t in TABLES:
        ddb.execute(f"COPY {t} TO '{os.path.join(parquet, t + '.parquet')}' (FORMAT PARQUET)")
    ddb.close()
    src.close()
    return ddb_path, parquet, q, a


@unittest.skipUnless(duckdb, "duckdb not installed")
class Duck(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ddb, self.parquet, self.q, self.a = _demo_duckdb(self.dir.name)
        self.questions = {x["id"]: x for x in core.load_questions(self.q)}
        self.answers, _ = core.load_answers(self.a)

    def tearDown(self):
        self.dir.cleanup()

    def test_scores_on_file_and_parquet_folder(self):
        # Same as SQLite except two demo queries written in SQLite-only SQL, which DuckDB rejects:
        # q09 selects an ungrouped column (gold fails, excluded) and q10 calls strftime on text.
        for source in (self.ddb, self.parquet):
            con = core.connect_readonly(source)
            try:
                results = core.score(con, list(self.questions.values()), self.answers)
            finally:
                con.close()
            s = core.summarise(results)
            verdicts = {r.id: r.verdict for r in results}
            self.assertEqual((s["passed"], s["silent_wrong"], s["loud_failures"], s["gold_errors"]), (5, 4, 2, 1))
            self.assertEqual((verdicts["q09"], verdicts["q10"]), ("gold_error", "sql_error"), source)

    def test_sandbox_blocks_writes_files_and_settings(self):
        con = core.connect_readonly(self.ddb)
        try:
            for sql in ["DELETE FROM customers", "SELECT 1; SELECT 2", "COPY customers TO 'x.csv'",
                        "SELECT * FROM read_csv('" + self.q.replace("'", "''") + "')",
                        "ATTACH 'other.duckdb'", "SET enable_external_access = true", "INSTALL httpfs"]:
                _, rows, err = core.run_sql(con, sql)
                self.assertIsNone(rows, sql)
                self.assertTrue(err, sql)
            _, _, err = core.run_sql(con, "SELECT COUNT(*) FROM range(100000000000) a, range(10) b", timeout=0.3)
            self.assertIn("timed out", err)
        finally:
            con.close()

    def test_shrink_blame_and_write_on_both_sources(self):
        for source in (self.ddb, self.parquet):
            got = {}
            for qid in ("q02", "q03", "q04", "q08"):
                cx = shrink.shrink(source, self.questions[qid], self.answers[qid])
                self.assertTrue(cx.one_minimal, (source, qid))
                b = blame.explain(source, self.questions[qid]["gold_sql"], self.answers[qid], cx)
                got[qid] = (cx.rows, b and b["clause"])
            self.assertEqual(got, {"q02": (1, "WHERE"), "q03": (4, "WHERE"), "q04": (2, "SELECT"),
                                   "q08": (3, "FROM")}, source)
        out = os.path.join(self.dir.name, "cx.duckdb")
        cx = shrink.shrink(self.ddb, self.questions["q08"], self.answers["q08"])
        shrink.write(cx, self.questions["q08"], self.answers["q08"], out)
        self.assertTrue(cx.verified)

    def test_cli_compare_and_exported_tests(self):
        locked = os.path.join(self.dir.name, "locked")
        code = main(["compare", "--db", self.parquet, "--old", self.questions["q04"]["gold_sql"],
                     "--new", self.answers["q04"], "--out", os.path.join(self.dir.name, "c.html"),
                     "--export-tests", locked])
        self.assertEqual(code, 1)
        with open(os.path.join(locked, "pair", "expected.json")) as fh:
            self.assertEqual(json.load(fh)["engine"], "duckdb")
        run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", locked],
                             capture_output=True, text=True)
        self.assertIn("failures=1", run.stderr)


if __name__ == "__main__":
    unittest.main()
