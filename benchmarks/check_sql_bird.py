"""Measure check_sql on BIRD dev: does it flag wrong answers, stay quiet on right ones,
and does applying its top suggestion fix the answer?

Uses the same inputs as benchmarks/bird.py (see benchmarks/README.md). Verdicts come from
goldcheck's comparison with the gold SQL. ASSISTANT_TESTED on public data.

Usage: python benchmarks/check_sql_bird.py --bird-dir BIRD --pred predict_dev.json --out results.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from goldcheck import core, probe  # noqa: E402

_CONS = {}


def _con(bird, db_id):
    if db_id not in _CONS:
        _CONS[db_id] = core.connect_readonly(os.path.join(bird, "dev_databases", db_id, f"{db_id}.sqlite"))
    return _CONS[db_id]


def _verdict(con, gold, sql, timeout):
    q = {"id": "q", "question": "", "gold_sql": gold}
    return core.score(con, [q], {"q": sql}, timeout)[0].verdict


def _one(job):
    bird, item, pred, timeout = job
    con = _con(bird, item["db_id"])
    started = time.monotonic()
    verdict = _verdict(con, item["SQL"], pred, timeout)
    out = {"id": item["question_id"], "db": item["db_id"], "verdict": verdict}
    if verdict not in core.SILENT and verdict != "pass":
        return out
    res = probe.check_sql(con, pred, timeout)
    kinds = [f["kind"] for f in res["findings"]]
    out.update(value_not_found=kinds.count("value_not_found"), excludes_nothing=kinds.count("filter_excludes_nothing"),
               empty_result="empty_result" in kinds, date_format=kinds.count("date_format_mismatch"),
               seconds=round(time.monotonic() - started, 2))
    # Apply the top suggestion for every flagged literal and re-score against gold.
    fixed = pred
    applied = 0
    for f in res["findings"]:
        if f["kind"] == "value_not_found" and f.get("suggestions"):
            old, new = "'" + f["literal"].replace("'", "''") + "'", "'" + f["suggestions"][0].replace("'", "''") + "'"
            if old in fixed:
                fixed, applied = fixed.replace(old, new), applied + 1
    if applied:
        out["after_fix"] = _verdict(con, item["SQL"], fixed, timeout)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bird-dir", required=True)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    with open(os.path.join(args.bird_dir, "dev_20240627", "dev.json"), encoding="utf-8") as fh:
        dev = {d["question_id"]: d for d in json.load(fh)}
    with open(args.pred, encoding="utf-8") as fh:
        raw = json.load(fh)
    preds = {int(k): v.split("\t----- bird -----\t")[0] for k, v in raw.items() if isinstance(v, str)}
    t0 = time.monotonic()
    with ProcessPoolExecutor(args.workers) as pool:
        rows = list(pool.map(_one, [(args.bird_dir, dev[i], preds[i], args.timeout) for i in sorted(preds)], chunksize=8))
    wrong = [r for r in rows if r["verdict"] in core.SILENT]
    right = [r for r in rows if r["verdict"] == "pass"]

    def rate(group, key):
        n = sum(1 for r in group if r.get(key))
        return {"count": n, "of": len(group), "share": round(n / len(group), 3) if group else None}

    fixed = [r for r in wrong if r.get("after_fix") == "pass"]
    broken = [r for r in right if r.get("after_fix") and r["after_fix"] != "pass"]
    flagged_wrong = [r for r in wrong if r.get("value_not_found")]
    summary = {
        "python": platform.python_version(), "platform": platform.platform(),
        "predictions_scored": len(rows), "silently_wrong": len(wrong), "correct": len(right),
        "value_not_found_on_wrong": rate(wrong, "value_not_found"),
        "value_not_found_on_correct": rate(right, "value_not_found"),
        "empty_result_on_wrong": rate(wrong, "empty_result"),
        "empty_result_on_correct": rate(right, "empty_result"),
        "date_format_mismatch_on_wrong": rate(wrong, "date_format"),
        "date_format_mismatch_on_correct": rate(right, "date_format"),
        "any_flag_on_wrong": {"count": sum(1 for r in wrong if r.get("value_not_found") or r.get("empty_result") or r.get("date_format")),
                              "of": len(wrong)},
        "any_flag_on_correct": {"count": sum(1 for r in right if r.get("value_not_found") or r.get("empty_result") or r.get("date_format")),
                                "of": len(right)},
        "precision_of_value_not_found": round(len(flagged_wrong) / max(1, len(flagged_wrong) + rate(right, "value_not_found")["count"]), 3),
        "top_suggestion_fixes_wrong_answer": {"count": len(fixed), "of_flagged_wrong": len(flagged_wrong),
                                              "of_all_wrong": len(wrong)},
        "top_suggestion_breaks_correct_answer": len(broken),
        "seconds": round(time.monotonic() - t0, 1),
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "rows": rows}, fh, indent=1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
