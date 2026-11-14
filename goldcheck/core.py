"""Deterministic scoring: run gold SQL and agent SQL read-only, compare result sets."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field

MAX_ROWS = 10000
ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}
VERDICTS = [
    "pass",
    "value_mismatch",
    "row_count_mismatch",
    "column_mismatch",
    "order_mismatch",
    "empty_result",
    "sql_error",
    "no_answer",
    "gold_error",
]
# Ran without error and returned something, but the something is wrong.
SILENT = {"value_mismatch", "row_count_mismatch", "column_mismatch", "order_mismatch", "empty_result"}


class InputError(Exception):
    """A problem with the files the user gave us; message is safe to show."""


def connect_readonly(path: str) -> sqlite3.Connection:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        raise InputError(f"Cannot open database read-only: {path} ({exc})") from exc

    def authorizer(action, *_):
        return sqlite3.SQLITE_OK if action in ALLOWED_ACTIONS else sqlite3.SQLITE_DENY

    con.set_authorizer(authorizer)
    return con


def run_sql(con, sql, timeout: float = 5.0):
    """Return (columns, rows, error). Exactly one of rows/error is set."""
    if sql is None or not str(sql).strip():
        return None, None, "no_answer"
    start = time.monotonic()
    con.set_progress_handler(lambda: 1 if time.monotonic() - start > timeout else 0, 5000)
    try:
        cur = con.execute(str(sql))
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS + 1)
        if len(rows) > MAX_ROWS:
            return None, None, f"more than {MAX_ROWS} rows returned"
        return cols, rows, None
    except sqlite3.Warning:
        return None, None, "only one SQL statement is allowed"
    except sqlite3.Error as exc:
        msg = str(exc)
        if "interrupted" in msg:
            msg = f"timed out after {timeout:g}s"
        elif "not authorized" in msg or "prohibited" in msg:
            msg = "statement is not read-only (blocked)"
        return None, None, msg
    finally:
        con.set_progress_handler(None, 0)


def norm(value):
    """Normalise one cell so 12, 12.0 and 12.0000001 compare equal."""
    if value is None:
        return ("null",)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        f = float(value)
        if math.isnan(f):
            return ("nan",)
        return ("num", float(f"{f:.6g}"))
    if isinstance(value, bytes):
        return ("bytes", value.hex())
    return ("text", str(value).strip())


def _signature(rows, j):
    return tuple(sorted(repr(norm(r[j])) for r in rows))


def compare(gold_cols, gold_rows, ans_cols, ans_rows, ordered: bool = False):
    """Return (verdict, detail). Column names and column order are ignored."""
    if not ans_rows and gold_rows:
        return "empty_result", f"agent returned 0 rows, expected {len(gold_rows)}"
    if len(ans_rows) != len(gold_rows):
        return "row_count_mismatch", f"agent returned {len(ans_rows)} rows, expected {len(gold_rows)}"
    n_gold, n_ans = len(gold_cols), len(ans_cols)
    ans_sigs = [_signature(ans_rows, j) for j in range(n_ans)]
    used, mapping = set(), []
    for j in range(n_gold):
        sig = _signature(gold_rows, j)
        hit = next((k for k in range(n_ans) if k not in used and ans_sigs[k] == sig), None)
        if hit is None:
            if n_ans < n_gold:
                return "column_mismatch", f"agent returned {n_ans} columns, expected {n_gold}"
            if len(gold_rows) == 1 and n_gold == 1 and n_ans == 1:
                return "value_mismatch", f"agent returned {ans_rows[0][0]!r}, expected {gold_rows[0][0]!r}"
            return "value_mismatch", f"no agent column holds the expected values of column {j + 1}"
        used.add(hit)
        mapping.append(hit)
    g = [tuple(norm(v) for v in r) for r in gold_rows]
    a = [tuple(norm(r[k]) for k in mapping) for r in ans_rows]
    if Counter(g) != Counter(a):
        return "value_mismatch", "columns match individually but rows pair values differently"
    if ordered and g != a:
        return "order_mismatch", "right rows, wrong order"
    extra = n_ans - n_gold
    return "pass", (f"{extra} extra column(s) ignored" if extra else "")


def wilson(passed: int, total: int, z: float = 1.96):
    """95% Wilson score interval for a pass rate."""
    if total == 0:
        return 0.0, 0.0
    p = passed / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Result:
    id: str
    question: str
    category: str
    gold_sql: str
    agent_sql: str
    verdict: str
    detail: str = ""
    gold_cols: list = field(default_factory=list)
    gold_rows: list = field(default_factory=list)
    agent_cols: list = field(default_factory=list)
    agent_rows: list = field(default_factory=list)


def load_questions(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise InputError(f"Cannot read questions file {path}: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise InputError("Questions file must be a non-empty JSON list.")
    seen = set()
    for i, q in enumerate(data):
        if not isinstance(q, dict) or not all(q.get(k) for k in ("id", "question", "gold_sql")):
            raise InputError(f"Question {i + 1} needs 'id', 'question' and 'gold_sql'.")
        q["id"] = str(q["id"])
        if q["id"] in seen:
            raise InputError(f"Duplicate question id: {q['id']}")
        seen.add(q["id"])
    return data


def load_answers(path: str):
    """JSON Lines: {"id": "...", "sql": "..."}; bad lines are reported, not fatal."""
    answers, problems = {}, []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise InputError(f"Cannot read answers file {path}: {exc}") from exc
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            answers[str(row["id"])] = row.get("sql")
        except (ValueError, KeyError, TypeError):
            problems.append(f"line {n}: not a JSON object with an 'id'")
    return answers, problems


def score(con, questions, answers, timeout: float = 5.0):
    results = []
    for q in questions:
        sql = answers.get(q["id"])
        r = Result(q["id"], q["question"], q.get("category", "uncategorised"), q["gold_sql"], sql or "", "pass")
        gc, gr, gerr = run_sql(con, q["gold_sql"], timeout)
        if gerr:
            r.verdict, r.detail = "gold_error", f"gold SQL failed: {gerr}"
            results.append(r)
            continue
        r.gold_cols, r.gold_rows = gc, gr
        ac, ar, aerr = run_sql(con, sql, timeout)
        if aerr == "no_answer":
            r.verdict, r.detail = "no_answer", "the agent gave no SQL for this question"
        elif aerr:
            r.verdict, r.detail = "sql_error", aerr
        else:
            r.agent_cols, r.agent_rows = ac, ar
            r.verdict, r.detail = compare(gc, gr, ac, ar, bool(q.get("ordered")))
        results.append(r)
    return results


def summarise(results):
    scored = [r for r in results if r.verdict != "gold_error"]
    passed = sum(r.verdict == "pass" for r in scored)
    low, high = wilson(passed, len(scored))
    by_cat = {}
    for r in scored:
        c = by_cat.setdefault(r.category, [0, 0])
        c[0] += r.verdict == "pass"
        c[1] += 1
    return {
        "total": len(scored),
        "passed": passed,
        "accuracy": passed / len(scored) if scored else 0.0,
        "ci_low": low,
        "ci_high": high,
        "silent_wrong": sum(r.verdict in SILENT for r in scored),
        "loud_failures": sum(r.verdict in ("sql_error", "no_answer") for r in scored),
        "gold_errors": len(results) - len(scored),
        "by_verdict": dict(Counter(r.verdict for r in results)),
        "by_category": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(by_cat.items())},
    }
