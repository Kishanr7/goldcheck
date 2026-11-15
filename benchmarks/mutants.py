"""Spike benchmark: shrink every silently wrong mutant of the demo's gold queries.

Mutants are produced mechanically (one operator, first occurrence) rather than written
by hand, to reduce cherry-picking. They are still assistant-authored and run on the
demo's fictional database: ASSISTANT_TESTED evidence only.

Usage: python benchmarks/mutants.py [--json results.json]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from goldcheck import core, demo, shrink  # noqa: E402

OPERATORS = [
    ("drop status filter", " WHERE o.status = 'completed'", ""),
    ("negate status", "= 'completed'", "!= 'completed'"),
    ("wrong status literal", "'completed'", "'complete'"),
    ("left join", " JOIN ", " LEFT JOIN "),
    ("sum to avg", "SUM(", "AVG("),
    ("sum to max", "SUM(", "MAX("),
    ("avg to sum", "AVG(", "SUM("),
    ("drop distinct", "COUNT(DISTINCT ", "COUNT("),
    ("count star to column", "COUNT(*)", "COUNT(id)"),
    ("strict lower bound", ">=", ">"),
    ("inclusive upper bound", "< '", "<= '"),
    ("date off by one", "'2026-04-01'", "'2026-03-31'"),
    ("reverse sort", "DESC", "ASC"),
    ("limit off by one", "LIMIT 3", "LIMIT 2"),
    ("drop tie-break", ", p.name LIMIT", " LIMIT"),
    ("price without qty", "oi.qty * oi.unit_price", "oi.unit_price"),
    ("wrong segment", "'Business'", "'Consumer'"),
    ("not in to in", "NOT IN", "IN"),
    ("join on customer id", "o.id = oi.order_id", "o.customer_id = oi.order_id"),
    ("join on order id", "c.id = o.customer_id", "c.id = o.id"),
    ("join product on order", "p.id = oi.product_id", "p.id = oi.order_id"),
    ("group by wrong key", "GROUP BY p.category", "GROUP BY p.id"),
    ("group by order", "GROUP BY o.id", "GROUP BY o.customer_id"),
]


def mutants():
    for qid, question, category, gold in demo.QUESTIONS:
        for name, old, new in OPERATORS:
            if old in gold:
                yield qid, question, gold, name, gold.replace(old, new, 1)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write per-mutant results here")
    ap.add_argument("--max-seconds", type=float, default=60.0)
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as d:
        db, _, _ = demo.build(d)
        con = core.connect_readonly(db)
        rows, total = [], 0
        for qid, question, gold, op, sql in mutants():
            total += 1
            q = {"id": f"{qid}:{op}", "question": question, "gold_sql": gold, "ordered": qid in demo.ORDERED}
            verdict = core.score(con, [q], {q["id"]: sql})[0].verdict
            if verdict not in core.SILENT:
                continue
            cx = shrink.shrink(db, q, sql, max_seconds=args.max_seconds)
            if cx is None:
                rows.append({"id": q["id"], "verdict": verdict, "shrunk": False})
                continue
            path = os.path.join(d, f"cx{len(rows)}.sqlite")
            shrink.write(cx, q, sql, path)
            rows.append({"id": q["id"], "verdict": verdict, "shrunk": True, "final_verdict": cx.verdict,
                         "source_rows": cx.source_rows, "rows": cx.rows, "checks": cx.checks,
                         "seconds": round(cx.seconds, 4), "one_minimal": cx.one_minimal,
                         "budget_exhausted": cx.budget_exhausted, "verified": cx.verified,
                         "tables": {k: len(v[1]) for k, v in cx.tables.items()}})
        con.close()
    silent = len(rows)
    ok = [r for r in rows if r["shrunk"] and r["verified"] and r["rows"] <= 10 and r["seconds"] <= args.max_seconds]
    sizes = [r["rows"] for r in rows if r["shrunk"]]
    summary = {
        "python": platform.python_version(), "platform": platform.platform(),
        "mutants": total, "silent_mutants": silent, "within_10_rows_and_60s": len(ok),
        "share": round(len(ok) / silent, 3) if silent else None,
        "median_rows": statistics.median(sizes) if sizes else None, "max_rows": max(sizes) if sizes else None,
        "max_seconds": max((r["seconds"] for r in rows if r["shrunk"]), default=None),
        "all_one_minimal": all(r.get("one_minimal") for r in rows if r["shrunk"]),
        "kill_criterion_passed": bool(silent) and len(ok) / silent >= 0.6,
    }
    print(json.dumps(summary, indent=2))
    for r in rows:
        print(f"{r['id']:<40} {r['verdict']:<20} " + (
            f"{r['source_rows']}->{r['rows']} rows {r['seconds']:.3f}s {r['tables']}" if r["shrunk"] else "NOT SHRUNK"))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "results": rows}, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
