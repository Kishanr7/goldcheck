import json
import os
import sqlite3
import tempfile
import unittest

from goldcheck import core, demo, report, shrink
from goldcheck.__main__ import main


class Compare(unittest.TestCase):
    def test_ignores_names_column_order_and_float_noise(self):
        v, _ = core.compare(["city", "rev"], [("A", 10.0), ("B", 2.5)], ["r", "c"], [(2.50000001, "B"), (10, "A")])
        self.assertEqual(v, "pass")

    def test_extra_columns_pass_with_note(self):
        v, d = core.compare(["n"], [(3,)], ["n", "x"], [(3, "junk")])
        self.assertEqual((v, d), ("pass", "1 extra column(s) ignored"))

    def test_wrong_value_names_both_numbers(self):
        v, d = core.compare(["n"], [(50,)], ["n"], [(300,)])
        self.assertEqual(v, "value_mismatch")
        self.assertIn("300", d)
        self.assertIn("50", d)

    def test_row_count_empty_missing_column_order(self):
        self.assertEqual(core.compare(["a"], [(1,), (2,)], ["a"], [(1,)])[0], "row_count_mismatch")
        self.assertEqual(core.compare(["a"], [(1,)], ["a"], [])[0], "empty_result")
        self.assertEqual(core.compare(["a", "b"], [(1, 2)], ["a"], [(1,)])[0], "column_mismatch")
        self.assertEqual(core.compare(["a"], [(1,), (2,)], ["a"], [(2,), (1,)], ordered=True)[0], "order_mismatch")
        self.assertEqual(core.compare(["a"], [(1,), (2,)], ["a"], [(2,), (1,)])[0], "pass")

    def test_rows_paired_differently_is_caught(self):
        v, _ = core.compare(["k", "v"], [("a", 1), ("b", 2)], ["k", "v"], [("a", 2), ("b", 1)])
        self.assertEqual(v, "value_mismatch")

    def test_null_is_not_zero(self):
        self.assertEqual(core.compare(["a"], [(None,)], ["a"], [(0,)])[0], "value_mismatch")

    def test_wilson(self):
        lo, hi = core.wilson(7, 12)
        self.assertAlmostEqual(lo, 0.32, places=2)
        self.assertAlmostEqual(hi, 0.81, places=2)
        self.assertEqual(core.wilson(0, 0), (0.0, 0.0))


class Safety(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db, self.q, self.a = demo.build(self.dir.name)
        self.con = core.connect_readonly(self.db)

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_writes_and_escapes_are_blocked(self):
        for sql in ["DELETE FROM customers", "DROP TABLE orders", "ATTACH DATABASE '/tmp/x.db' AS x",
                    "PRAGMA writable_schema=1", "SELECT 1; DELETE FROM customers"]:
            _, rows, err = core.run_sql(self.con, sql)
            self.assertIsNone(rows, sql)
            self.assertTrue(err, sql)
        check = sqlite3.connect(self.db)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 60)
        check.close()

    def test_runaway_query_times_out(self):
        sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c"
        _, _, err = core.run_sql(self.con, sql, timeout=0.3)
        self.assertIn("timed out", err)

    def test_demo_is_deterministic_and_scores_as_documented(self):
        answers, problems = core.load_answers(self.a)
        s = core.summarise(core.score(self.con, core.load_questions(self.q), answers))
        self.assertEqual(problems, [])
        self.assertEqual((s["passed"], s["total"], s["silent_wrong"], s["loud_failures"]), (7, 12, 4, 1))

    def test_missing_and_malformed_answers(self):
        bad = os.path.join(self.dir.name, "bad.jsonl")
        with open(bad, "w") as fh:
            fh.write('{"id": "q01", "sql": "SELECT COUNT(*) FROM customers"}\nnot json\n{"sql": "x"}\n\n')
        answers, problems = core.load_answers(bad)
        self.assertEqual(len(problems), 2)
        results = core.score(self.con, core.load_questions(self.q), answers)
        self.assertEqual(sum(r.verdict == "no_answer" for r in results), 11)

    def test_gold_error_is_excluded_not_counted_against_agent(self):
        qs = [{"id": "x", "question": "?", "gold_sql": "SELECT nope FROM nowhere"}]
        s = core.summarise(core.score(self.con, qs, {"x": "SELECT 1"}))
        self.assertEqual((s["total"], s["gold_errors"]), (0, 1))

    def test_report_escapes_html_and_handles_empty(self):
        qs = [{"id": "x", "question": "<script>alert(1)</script>", "gold_sql": "SELECT '<b>'"}]
        res = core.score(self.con, qs, {"x": "SELECT '<b>'"})
        html = report.render(res, core.summarise(res), "<t>")
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Nothing could be scored", report.render([], core.summarise([])))


class Shrink(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db, qpath, apath = demo.build(self.dir.name)
        self.questions = {q["id"]: q for q in core.load_questions(qpath)}
        self.answers, _ = core.load_answers(apath)

    def tearDown(self):
        self.dir.cleanup()

    def _digest(self):
        import hashlib
        with open(self.db, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def test_demo_silent_failures_shrink_to_tiny_verified_databases(self):
        before = self._digest()
        source = shrink.load_source(self.db)
        expected = {"q02": 1, "q03": 2, "q04": 2, "q08": 3}
        for qid, rows in expected.items():
            cx = shrink.shrink(self.db, self.questions[qid], self.answers[qid], source=source)
            self.assertEqual(cx.rows, rows, qid)
            self.assertIn(cx.verdict, core.SILENT, qid)
            self.assertTrue(cx.one_minimal and not cx.budget_exhausted, qid)
            out = os.path.join(self.dir.name, f"{qid}.sqlite")
            shrink.write(cx, self.questions[qid], self.answers[qid], out)
            self.assertTrue(cx.verified, qid)
            with self.assertRaises(core.InputError):
                shrink.write(cx, self.questions[qid], self.answers[qid], out)
        self.assertEqual(self._digest(), before, "source database must not change")

    def test_nothing_to_shrink_for_passes_and_errors(self):
        self.assertIsNone(shrink.shrink(self.db, self.questions["q01"], self.answers["q01"]))
        self.assertIsNone(shrink.shrink(self.db, self.questions["q07"], self.answers["q07"]))  # agent SQL errors

    def test_agent_sql_stays_read_only_on_candidates(self):
        q = {"id": "x", "question": "?", "gold_sql": "SELECT COUNT(*) FROM customers"}
        self.assertIsNone(shrink.shrink(self.db, q, "DELETE FROM customers"))
        self.assertIsNone(shrink.shrink(self.db, q, "ATTACH DATABASE 'x.db' AS x"))

    def test_budget_is_reported_not_hidden(self):
        cx = shrink.shrink(self.db, self.questions["q08"], self.answers["q08"], max_checks=3)
        self.assertTrue(cx.budget_exhausted)
        self.assertFalse(cx.one_minimal)
        self.assertIn(cx.verdict, core.SILENT)

    def test_declared_foreign_keys_are_kept(self):
        path = os.path.join(self.dir.name, "fk.sqlite")
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE parent (id INTEGER PRIMARY KEY);"
                          "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));"
                          "INSERT INTO parent VALUES (1),(2),(3);"
                          "INSERT INTO child VALUES (1,1),(2,2),(3,3),(4,1);")
        con.commit()
        con.close()
        q = {"id": "fk", "question": "?", "gold_sql": "SELECT COUNT(*) FROM child"}
        cx = shrink.shrink(path, q, "SELECT COUNT(*) FROM child WHERE id > 99")
        self.assertEqual(sorted(cx.tables), ["child", "parent"])
        self.assertEqual(cx.rows, 2)

    def test_report_shows_counterexample_escaped(self):
        con = core.connect_readonly(self.db)
        results = core.score(con, list(self.questions.values()), self.answers)
        con.close()
        r = next(r for r in results if r.id == "q08")
        r.counterexample = shrink.shrink(self.db, self.questions["q08"], self.answers["q08"])
        r.counterexample.tables["customers"][1][0] = (13, "<img src=x>", "Northwick", "Consumer")
        html = report.render(results, core.summarise(results))
        self.assertIn("Smallest database that shows the difference", html)
        self.assertIn("3 rows instead of 992", html)
        self.assertNotIn("<img src=x>", html)
        self.assertIn("INSERT INTO", html)


class Cli(unittest.TestCase):
    def test_demo_and_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(["demo", "--out", d]), 0)
            self.assertTrue(os.path.getsize(os.path.join(d, "report.html")) > 2000)
            args = ["run", "--db", os.path.join(d, "demo.sqlite"), "--questions", os.path.join(d, "questions.json"),
                    "--answers", os.path.join(d, "flawed_agent.jsonl"), "--out", os.path.join(d, "r.html")]
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args + ["--min-accuracy", "0.9"]), 1)
            self.assertEqual(main(args + ["--shrink"]), 0)
            with open(os.path.join(d, "r.html"), encoding="utf-8") as fh:
                self.assertEqual(fh.read().count("Smallest database that shows the difference"), 4)
            common = ["shrink", "--db", os.path.join(d, "demo.sqlite"), "--questions",
                      os.path.join(d, "questions.json"), "--answers", os.path.join(d, "flawed_agent.jsonl")]
            self.assertEqual(main(common + ["--id", "q04", "--out", os.path.join(d, "cx.sqlite")]), 0)
            self.assertEqual(main(common + ["--id", "q04", "--out", os.path.join(d, "cx.sqlite")]), 2)
            self.assertEqual(main(common + ["--id", "q01", "--out", os.path.join(d, "cx2.sqlite")]), 1)

    def test_agent_command_adapter(self):
        with tempfile.TemporaryDirectory() as d:
            db, q, _ = demo.build(d)
            out = os.path.join(d, "r.html")
            code = main(["run", "--db", db, "--questions", q, "--out", out, "--json",
                         "--agent-cmd", "python3 -c \"print('SELECT COUNT(*) FROM customers')\""])
            self.assertEqual(code, 0)

    def test_bad_inputs_give_clear_errors(self):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.json")
            with open(q, "w") as fh:
                json.dump([{"id": 1}], fh)
            self.assertEqual(main(["run", "--db", os.path.join(d, "missing.sqlite"), "--questions", q, "--answers", q]), 2)


if __name__ == "__main__":
    unittest.main()
