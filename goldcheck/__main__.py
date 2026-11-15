"""Command line: python -m goldcheck demo | run | compare | shrink | mcp"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys

from . import __version__
from .core import SILENT, InputError, connect_readonly, load_answers, load_pairs, load_questions, score, summarise
from .report import render


def _ask_agent(cmd: str, questions, timeout: float):
    """Run a user-supplied command once per question; its stdout is taken as the SQL."""
    answers = {}
    for q in questions:
        argv = [a.replace("{question}", q["question"]) for a in shlex.split(cmd)]
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            answers[q["id"]] = out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            answers[q["id"]] = None
    return answers


def _shrink_all(db, questions, results, max_seconds, timeout=5.0):
    """Attach a counterexample to every silent failure. Returns the results that got one."""
    from . import blame, shrink
    by_id = {q["id"]: q for q in questions}
    found = []
    for r in results:
        if r.verdict in SILENT:
            r.counterexample = shrink.shrink(db, by_id[r.id], r.agent_sql, timeout=timeout,
                                             max_seconds=max_seconds)
            if r.counterexample:
                r.counterexample.blame = blame.explain(db, r.gold_sql, r.agent_sql, r.counterexample,
                                                       r.counterexample.ordered, timeout)
                found.append(r)
    if found:
        sizes = sorted(r.counterexample.rows for r in found)
        span = f"{sizes[0]}" if sizes[0] == sizes[-1] else f"{sizes[0]}-{sizes[-1]}"
        print(f"Shrunk {len(found)} silent difference(s) to {span} row(s) each; see the report.")
    return found


def _export(results, out_dir):
    from . import lock
    try:
        n = lock.export(results, out_dir)
    except FileExistsError as exc:
        raise InputError(str(exc)) from exc
    print(f"Wrote {n} regression test case(s) to {os.path.abspath(out_dir)} "
          f"(run: python -m unittest discover -s \"{out_dir}\").")


def _finish(results, problems, out, title, min_accuracy, as_json, mode="check"):
    summary = summarise(results)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render(results, summary, title, problems, mode))
    if as_json:
        print(json.dumps(summary, indent=2))
    elif mode == "compare":
        differ = summary["total"] - summary["passed"]
        print(f"{summary['passed']}/{summary['total']} pair(s) return the same result; {differ} differ "
              f"({summary['silent_wrong']} silently, {summary['loud_failures']} with an error in the new query).")
        for r in results:
            if r.verdict != "pass":
                cx = r.counterexample
                extra = f" Smallest database: {cx.rows} row(s)." if cx else ""
                if cx and cx.blame:
                    extra += f" Comes from the {cx.blame['clause']} clause."
                print(f"  {r.id}: {r.detail or r.verdict}.{extra}")
        for p in problems:
            print(f"warning: {p}")
        print(f"Report: {os.path.abspath(out)}")
        return 0 if summary["passed"] == summary["total"] else 1
    else:
        print(f"{summary['passed']}/{summary['total']} correct "
              f"({round(100 * summary['accuracy'])}%, 95% interval "
              f"{round(100 * summary['ci_low'])}-{round(100 * summary['ci_high'])}%)")
        print(f"{summary['silent_wrong']} ran cleanly but returned the wrong result; "
              f"{summary['loud_failures']} failed visibly.")
        for p in problems:
            print(f"warning: {p}")
        print(f"Report: {os.path.abspath(out)}")
    return 0 if summary["accuracy"] >= min_accuracy else 1


def _shrink_one(args, questions, con):
    from . import shrink
    answers, problems = load_answers(args.answers)
    for p in problems:
        print(f"warning: {p}")
    q = next((q for q in questions if q["id"] == args.id), None)
    if q is None:
        raise InputError(f"No question with id {args.id}")
    if os.path.exists(args.out):
        raise InputError(f"Refusing to overwrite {args.out}; choose a new path.")
    r = score(con, [q], answers, args.timeout)[0]
    if r.verdict not in SILENT:
        print(f"{args.id}: verdict is '{r.verdict}', not a silent wrong answer; nothing to shrink.")
        return 1
    cx = shrink.shrink(args.db, q, r.agent_sql, timeout=args.timeout, max_seconds=args.max_seconds)
    if cx is None:
        print(f"{args.id}: could not reproduce the disagreement on a copy of the database.")
        return 1
    shrink.write(cx, q, r.agent_sql, args.out, args.timeout)
    counts = ", ".join(f"{name} {len(rows)}" for name, (_, rows) in cx.tables.items())
    print(f"{args.id}: {cx.source_rows} rows -> {cx.rows} ({counts}) in {cx.checks} checks, {cx.seconds:.2f}s.")
    print(f"On these rows: {cx.detail}.")
    print("No single row can be removed." if cx.one_minimal else "Budget ran out; it may shrink further.")
    print(f"Wrote {os.path.abspath(args.out)} ({'re-checked' if cx.verified else 'WARNING: re-check failed'}).")
    return 0 if cx.verified else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="goldcheck", description="Find, explain and lock down SQL that returns the wrong result.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="build a fictional database and score a deliberately flawed agent")
    d.add_argument("--out", default="goldcheck-demo", help="folder for demo files and report")
    r = sub.add_parser("run", help="score your own agent")
    r.add_argument("--db", required=True, help="SQLite database file (opened read-only)")
    r.add_argument("--questions", required=True, help="JSON list of {id, question, gold_sql}")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--answers", help="JSON Lines file of {id, sql} from your agent")
    src.add_argument("--agent-cmd", help="command run per question; use {question}; stdout is the SQL")
    r.add_argument("--out", default="goldcheck-report.html")
    r.add_argument("--title", default="Agent evaluation")
    r.add_argument("--timeout", type=float, default=5.0, help="seconds per query")
    r.add_argument("--agent-timeout", type=float, default=60.0, help="seconds per agent call")
    r.add_argument("--min-accuracy", type=float, default=0.0, help="exit 1 below this (0-1), for CI")
    r.add_argument("--json", action="store_true", help="print the summary as JSON")
    r.add_argument("--shrink", action="store_true",
                   help="for each silently wrong answer, find the smallest database that shows it")
    r.add_argument("--shrink-seconds", type=float, default=60.0, help="time budget per question for --shrink")
    r.add_argument("--export-tests", metavar="DIR", help="with --shrink: write each counterexample as a "
                   "standalone regression test into this new folder")
    k = sub.add_parser("shrink", help="write the smallest database on which one agent answer is wrong")
    k.add_argument("--db", required=True, help="SQLite database file (opened read-only)")
    k.add_argument("--questions", required=True, help="JSON list of {id, question, gold_sql}")
    k.add_argument("--answers", required=True, help="JSON Lines file of {id, sql} from your agent")
    k.add_argument("--id", required=True, help="question id to shrink")
    k.add_argument("--out", required=True, help="new .sqlite file to write (never overwritten)")
    k.add_argument("--timeout", type=float, default=5.0, help="seconds per query")
    k.add_argument("--max-seconds", type=float, default=60.0, help="time budget for the search")
    c = sub.add_parser("compare", help="check that a rewritten query returns what the old one did; no gold needed")
    c.add_argument("--db", required=True, help="SQLite database file (opened read-only)")
    one = c.add_mutually_exclusive_group(required=True)
    one.add_argument("--old", help="old query: a .sql file, or the SQL itself (use with --new)")
    one.add_argument("--pairs", help="JSON Lines of {id, old, new, note?, ordered?}")
    c.add_argument("--new", help="new query: a .sql file, or the SQL itself")
    c.add_argument("--ordered", action="store_true", help="row order matters (single pair)")
    c.add_argument("--out", default="goldcheck-compare.html")
    c.add_argument("--title", default="Old vs new query")
    c.add_argument("--timeout", type=float, default=5.0, help="seconds per query")
    c.add_argument("--no-shrink", action="store_true", help="skip finding the smallest database for each difference")
    c.add_argument("--shrink-seconds", type=float, default=60.0, help="time budget per pair")
    c.add_argument("--json", action="store_true", help="print the summary as JSON")
    c.add_argument("--export-tests", metavar="DIR", help="write each counterexample as a standalone "
                   "regression test into this new folder")
    sub.add_parser("mcp", help="serve compare_queries to coding agents over MCP (stdio)")
    args = ap.parse_args(argv)
    if args.cmd == "mcp":
        from .mcp import serve
        serve()
        return 0
    if args.cmd == "compare" and args.old and not args.new:
        ap.error("--old needs --new")
    if getattr(args, "export_tests", None) and not (getattr(args, "shrink", False)
                                                    or (args.cmd == "compare" and not args.no_shrink)):
        ap.error("--export-tests needs shrinking (add --shrink, or drop --no-shrink)")
    con = None
    try:
        if args.cmd == "demo":
            from .demo import build
            db, qpath, apath = build(args.out)
            questions = load_questions(qpath)
            answers, problems = load_answers(apath)
            con = connect_readonly(db)
            results = score(con, questions, answers)
            _shrink_all(db, questions, results, 60.0)
            _finish(results, problems, os.path.join(args.out, "report.html"),
                    "Demo: a careless agent on a fictional shop", 0.0, False)
            print("Open the report in a browser. Then try `python -m goldcheck run --help` with your own agent.")
            return 0
        if args.cmd == "compare":
            questions, answers = load_pairs(args.pairs, args.old, args.new, args.ordered)
            con = connect_readonly(args.db)
            results = score(con, questions, answers, args.timeout)
            if not args.no_shrink:
                _shrink_all(args.db, questions, results, args.shrink_seconds, args.timeout)
            if args.export_tests:
                _export(results, args.export_tests)
            return _finish(results, [], args.out, args.title, 0.0, args.json, mode="compare")
        questions = load_questions(args.questions)
        con = connect_readonly(args.db)
        if args.cmd == "shrink":
            return _shrink_one(args, questions, con)
        if args.answers:
            answers, problems = load_answers(args.answers)
        else:
            answers, problems = _ask_agent(args.agent_cmd, questions, args.agent_timeout), []
        unknown = sorted(set(answers) - {q["id"] for q in questions})
        if unknown:
            problems.append(f"answers for unknown question ids ignored: {', '.join(unknown[:5])}")
        results = score(con, questions, answers, args.timeout)
        if args.shrink:
            _shrink_all(args.db, questions, results, args.shrink_seconds, args.timeout)
            if args.export_tests:
                _export(results, args.export_tests)
        return _finish(results, problems, args.out, args.title, args.min_accuracy, args.json)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    finally:
        if con is not None:
            con.close()  # Windows cannot delete an open database file


if __name__ == "__main__":
    sys.exit(main())
