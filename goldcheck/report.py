"""One self-contained HTML file. No scripts, no network requests."""
from __future__ import annotations

from html import escape

LABELS = {
    "pass": "Correct",
    "value_mismatch": "Wrong values",
    "row_count_mismatch": "Wrong number of rows",
    "column_mismatch": "Missing columns",
    "order_mismatch": "Wrong order",
    "empty_result": "Returned nothing",
    "sql_error": "SQL failed",
    "no_answer": "No answer",
    "gold_error": "Gold SQL failed",
}
SILENT = {"value_mismatch", "row_count_mismatch", "column_mismatch", "order_mismatch", "empty_result"}

CSS = """
:root{--bg:#f7f6f2;--ink:#1d2129;--mute:#5b6270;--line:#d9d6cc;--card:#fff;--ok:#1a6b3c;--bad:#a4281c;--warn:#8a5a00;--accent:#243b6b}
@media (prefers-color-scheme:dark){:root{--bg:#15171c;--ink:#e9e7e1;--mute:#a3a8b3;--line:#333844;--card:#1d2027;--ok:#6fcf97;--bad:#ff8a7a;--warn:#f0c35b;--accent:#9db7f0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 Georgia,'Times New Roman',serif}
main{max-width:980px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:1.7rem;margin:0 0 4px}h2{font-size:1.15rem;margin:34px 0 10px}
.tag{display:inline-block;font:600 .72rem/1 system-ui,sans-serif;letter-spacing:.08em;text-transform:uppercase;border:1px solid var(--line);padding:5px 8px;border-radius:3px;color:var(--mute)}
.lede{color:var(--mute);margin:6px 0 22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:14px 16px}
.stat b{display:block;font:700 2rem/1.1 system-ui,sans-serif}.stat span{color:var(--mute);font-size:.92rem}
.stat.bad b{color:var(--bad)}.stat.ok b{color:var(--ok)}
table{width:100%;border-collapse:collapse;font:.9rem/1.4 system-ui,sans-serif}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600}.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:6px}
details{background:var(--card);border:1px solid var(--line);border-radius:6px;margin:8px 0}
summary{cursor:pointer;padding:11px 14px;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
summary:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.v{font:700 .78rem/1 system-ui,sans-serif;padding:4px 7px;border-radius:3px;border:1px solid currentColor;white-space:nowrap}
.v.pass{color:var(--ok)}.v.silent{color:var(--bad)}.v.loud{color:var(--warn)}
.body{padding:0 14px 14px}.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:10px;overflow-x:auto;font:.82rem/1.45 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word;margin:6px 0}
h3{font:600 .8rem/1 system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:12px 0 4px}
.note{color:var(--mute);font-size:.9rem}footer{margin-top:40px;color:var(--mute);font-size:.88rem;border-top:1px solid var(--line);padding-top:14px}
"""


def _table(cols, rows, limit=6):
    if not cols:
        return "<p class='note'>No result.</p>"
    head = "".join(f"<th scope='col'>{escape(str(c))}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in r) + "</tr>" for r in rows[:limit])
    more = f"<p class='note'>Showing {limit} of {len(rows)} rows.</p>" if len(rows) > limit else ""
    return f"<div class='wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}"


def _kind(verdict):
    return "pass" if verdict == "pass" else ("silent" if verdict in SILENT else "loud")


def render(results, summary, title="Agent evaluation", problems=()):
    s = summary
    pct = lambda x: f"{round(100 * x)}%"
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>{escape(title)}</title><style>{CSS}</style></head><body><main>"]
    parts.append(f"<span class='tag'>goldcheck report</span><h1>{escape(title)}</h1>")
    if s["total"] == 0:
        parts.append("<p class='lede'>Nothing could be scored. Check the questions file and the gold SQL.</p>")
    else:
        parts.append(
            f"<p class='lede'>The agent answered {s['passed']} of {s['total']} questions correctly. "
            f"With this few questions the true rate is plausibly anywhere from {pct(s['ci_low'])} to "
            f"{pct(s['ci_high'])} (95% interval).</p>")
        parts.append("<div class='grid'>"
                     f"<div class='stat ok'><b>{pct(s['accuracy'])}</b><span>answers correct</span></div>"
                     f"<div class='stat bad'><b>{s['silent_wrong']}</b><span>ran cleanly, wrong result — nobody would notice</span></div>"
                     f"<div class='stat'><b>{s['loud_failures']}</b><span>failed visibly (error or no answer)</span></div>"
                     "</div>")
        rows = "".join(
            f"<tr><th scope='row'>{escape(c)}</th><td>{v['passed']} of {v['total']}</td></tr>"
            for c, v in s["by_category"].items())
        parts.append("<h2>By question type</h2><div class='wrap'><table><thead><tr><th scope='col'>Type</th>"
                     f"<th scope='col'>Correct</th></tr></thead><tbody>{rows}</tbody></table></div>")
    if problems:
        items = "".join(f"<li>{escape(p)}</li>" for p in problems)
        parts.append(f"<h2>Problems in the answers file</h2><ul>{items}</ul>")
    if s["gold_errors"]:
        parts.append(f"<p class='note'>{s['gold_errors']} question(s) were excluded because their gold SQL failed.</p>")
    parts.append("<h2>Every question</h2>")
    order = {"silent": 0, "loud": 1, "pass": 2}
    for r in sorted(results, key=lambda r: (order[_kind(r.verdict)], r.id)):
        kind = _kind(r.verdict)
        parts.append(
            f"<details{' open' if kind == 'silent' else ''}><summary><span class='v {kind}'>{LABELS[r.verdict]}</span>"
            f"<span>{escape(r.id)} · {escape(r.question)}</span></summary><div class='body'>"
            + (f"<p class='note'>{escape(r.detail)}</p>" if r.detail else "")
            + "<div class='cols'><div><h3>Expected</h3>"
            f"<pre>{escape(r.gold_sql)}</pre>{_table(r.gold_cols, r.gold_rows)}</div>"
            f"<div><h3>Agent</h3><pre>{escape(r.agent_sql) or '(no SQL)'}</pre>{_table(r.agent_cols, r.agent_rows)}</div>"
            "</div></div></details>")
    parts.append("<footer>Scoring is deterministic: both queries run read-only against the same database and "
                 "their results are compared, ignoring column names, column order and tiny numeric differences. "
                 "No model is involved in scoring and nothing left this computer. "
                 "A pass means the result matched the gold answer, not that the gold answer is right.</footer>"
                 "</main></body></html>")
    return "".join(parts)
