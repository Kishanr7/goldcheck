"""Shrink real model errors on the BIRD dev set.

Inputs (not in this repo; see benchmarks/README.md):
  --bird-dir  folder holding dev_20240627/dev.json and dev_databases/<db>/<db>.sqlite
  --pred      official BIRD baseline predictions (predict_dev.json: "SQL\\t----- bird -----\\tdb_id")

Step 1 scores every prediction with goldcheck's comparison. Step 2 shrinks a seeded random
sample of the silently wrong ones. Results are ASSISTANT_TESTED on public data with real
model output; they say nothing about whether anyone wants the feature.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from goldcheck import blame, core, shrink  # noqa: E402


def _db(bird, db_id):
    return os.path.join(bird, "dev_databases", db_id, f"{db_id}.sqlite")


def _classify(job):
    bird, item, pred, timeout = job
    con = core.connect_readonly(_db(bird, item["db_id"]))
    try:
        q = {"id": str(item["question_id"]), "question": item["question"], "gold_sql": item["SQL"]}
        r = core.score(con, [q], {q["id"]: pred}, timeout)[0]
        gold = {tuple(x) for x in r.gold_rows} if r.gold_cols else None
        agent = {tuple(x) for x in r.agent_rows} if r.agent_cols else None
        bird_ex = gold is not None and agent is not None and gold == agent  # BIRD's own set comparison
        return {"id": q["id"], "db": item["db_id"], "difficulty": item.get("difficulty"),
                "verdict": r.verdict, "bird_ex": bird_ex}
    finally:
        con.close()


def _shrink(job):
    bird, item, pred, timeout, max_seconds = job
    q = {"id": str(item["question_id"]), "question": item["question"], "gold_sql": item["SQL"]}
    started = time.monotonic()
    try:
        cx = shrink.shrink(_db(bird, item["db_id"]), q, pred, timeout=timeout, max_seconds=max_seconds)
    except Exception as exc:  # record, do not hide
        return {"id": q["id"], "db": item["db_id"], "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.monotonic() - started, 2)}
    if cx is None:
        return {"id": q["id"], "db": item["db_id"], "status": "not_reproduced",
                "seconds": round(time.monotonic() - started, 2)}
    with tempfile.TemporaryDirectory() as d:
        shrink.write(cx, q, pred, os.path.join(d, "cx.sqlite"), timeout)
    b = blame.explain(_db(bird, item["db_id"]), item["SQL"], pred, cx, False, timeout)
    detail = {"question": item["question"], "evidence": item.get("evidence"), "gold": item["SQL"], "pred": pred,
              "tables": {k: [v[0], [list(map(str, r)) for r in v[1]][:12]] for k, v in cx.tables.items()},
              "gold_result": [list(map(str, r)) for r in cx.gold[1]][:6],
              "pred_result": [list(map(str, r)) for r in cx.agent[1]][:6], "detail": cx.detail}
    return {"blame": b and b["clause"], "blame_full_db": b and b["full_db"], "_detail": detail,"id": q["id"], "db": item["db_id"], "difficulty": item.get("difficulty"), "status": "shrunk",
            "final_verdict": cx.verdict, "source_rows": cx.source_rows, "rows": cx.rows,
            "context_rows": cx.context_rows, "checks": cx.checks, "seconds": round(cx.seconds, 2),
            "one_minimal": cx.one_minimal, "budget_exhausted": cx.budget_exhausted, "verified": cx.verified,
            "tables": {k: len(v[1]) for k, v in cx.tables.items()}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bird-dir", required=True)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--seed", type=int, default=20261116)
    ap.add_argument("--timeout", type=float, default=10.0, help="seconds per query")
    ap.add_argument("--max-seconds", type=float, default=60.0, help="shrink budget per question")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", required=True, help="results JSON")
    ap.add_argument("--details", help="also write questions, SQL and counterexample rows here (keep local)")
    ap.add_argument("--one", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.one:
        with open(os.path.join(args.bird_dir, "dev_20240627", "dev.json"), encoding="utf-8") as fh:
            item = next(d for d in json.load(fh) if str(d["question_id"]) == args.one)
        with open(args.pred, encoding="utf-8") as fh:
            pred = json.load(fh)[args.one].split("	----- bird -----	")[0]
        print(json.dumps(_shrink((args.bird_dir, item, pred, args.timeout, args.max_seconds))))
        return 0

    with open(os.path.join(args.bird_dir, "dev_20240627", "dev.json"), encoding="utf-8") as fh:
        dev = json.load(fh)
    with open(args.pred, encoding="utf-8") as fh:
        raw = json.load(fh)
    # Non-string entries (the official file has 54 zeros) mean the model gave no SQL: scored as no_answer.
    preds = {int(k): (v.split("\t----- bird -----\t")[0] if isinstance(v, str) else None) for k, v in raw.items()}
    items = {d["question_id"]: d for d in dev}

    t0 = time.monotonic()
    with ProcessPoolExecutor(args.workers) as pool:
        classified = list(pool.map(_classify, [(args.bird_dir, items[i], preds[i], args.timeout) for i in sorted(preds)],
                                   chunksize=8))
    classify_seconds = time.monotonic() - t0
    silent = [c for c in classified if c["verdict"] in core.SILENT]
    rng = random.Random(args.seed)
    sample = sorted(rng.sample(silent, min(args.sample, len(silent))), key=lambda c: int(c["id"]))

    t0 = time.monotonic()
    hard = 3 * args.max_seconds + 60  # wall clock per question, including copy and seeding

    def one(c):
        cmd = [sys.executable, os.path.abspath(__file__), "--one", c["id"], "--bird-dir", args.bird_dir,
               "--pred", args.pred, "--timeout", str(args.timeout), "--max-seconds", str(args.max_seconds),
               "--out", "-"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=hard)
            return json.loads(out.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return {"id": c["id"], "db": c["db"], "status": "hard_timeout", "seconds": hard}
        except (ValueError, IndexError):
            return {"id": c["id"], "db": c["db"], "status": "error", "error": (out.stderr or "")[-300:]}

    with ThreadPoolExecutor(args.workers) as pool:
        shrunk = list(pool.map(one, sample))
    shrink_seconds = time.monotonic() - t0
    details = {r["id"]: r.pop("_detail") for r in shrunk if "_detail" in r}

    ok = [r for r in shrunk if r["status"] == "shrunk"]
    good = [r for r in ok if r["verified"] and r["rows"] <= 10 and r["seconds"] <= args.max_seconds]
    verdicts = {}
    for c in classified:
        verdicts[c["verdict"]] = verdicts.get(c["verdict"], 0) + 1
    agree = sum((c["verdict"] == "pass") == c["bird_ex"] for c in classified)
    summary = {
        "python": platform.python_version(), "platform": platform.platform(), "pred_file": os.path.basename(args.pred),
        "predictions": len(classified), "goldcheck_verdicts": verdicts,
        "goldcheck_vs_bird_ex_agreement": round(agree / len(classified), 3),
        "silent_wrong": len(silent), "sampled": len(sample),
        "shrunk": len(ok), "not_reproduced": sum(r["status"] == "not_reproduced" for r in shrunk),
        "errors": sum(r["status"] == "error" for r in shrunk),
        "within_10_rows_and_budget_verified": len(good),
        "share_of_sample": round(len(good) / len(sample), 3) if sample else None,
        "median_rows": statistics.median(r["rows"] for r in ok) if ok else None,
        "p90_rows": sorted(r["rows"] for r in ok)[int(0.9 * (len(ok) - 1))] if ok else None,
        "median_seconds": statistics.median(r["seconds"] for r in ok) if ok else None,
        "budget_exhausted": sum(r.get("budget_exhausted", False) for r in ok),
        "one_minimal": sum(r.get("one_minimal", False) for r in ok),
        "hard_timeouts": sum(r["status"] == "hard_timeout" for r in shrunk),
        "blamed_one_clause": sum(bool(r.get("blame")) for r in ok),
        "blame_confirmed_full_db": sum(bool(r.get("blame_full_db")) for r in ok),
        "blame_by_clause": {k: sum(r.get("blame") == k for r in ok) for k in blame.TRY_ORDER},
        "kill_criterion_passed": bool(sample) and len(good) / len(sample) >= 0.6,
        "classify_seconds": round(classify_seconds, 1), "shrink_seconds": round(shrink_seconds, 1),
        "settings": {"sample": args.sample, "seed": args.seed, "timeout": args.timeout,
                     "max_seconds": args.max_seconds, "workers": args.workers},
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "shrink": shrunk, "classified": classified}, fh, indent=1)
    if args.details:
        with open(args.details, "w", encoding="utf-8") as fh:
            json.dump(details, fh, indent=1, ensure_ascii=False)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
