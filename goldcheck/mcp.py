"""A minimal MCP server over stdio, standard library only: `python -m goldcheck mcp`.

One tool, `compare_queries`, lets a coding agent check its own SQL against a trusted query
on a local SQLite database and get back the difference, the smallest database that shows
it, and the clause that causes it. The agent can then repair its query and call again.

Messages are newline-delimited JSON-RPC 2.0, as the MCP stdio transport specifies.
"""
from __future__ import annotations

import json
import sys

from . import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [{
    "name": "compare_queries",
    "description": (
        "Run a trusted SQL query and a query to check on the same local SQLite database (read-only) and "
        "report whether they return the same result. If they differ silently, also return the smallest set "
        "of real rows on which they still differ, the clause of the checked query that causes it, and a SQL "
        "script that recreates those rows. Use it to verify SQL you wrote or rewrote before trusting it."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "db_path": {"type": "string", "description": "SQLite or DuckDB file, Parquet/CSV/JSON file or folder, "
                                                         "or a postgresql:// URL"},
            "trusted_sql": {"type": "string", "description": "The query whose result is known to be right"},
            "sql": {"type": "string", "description": "The query to check"},
            "ordered": {"type": "boolean", "description": "Row order matters", "default": False},
            "shrink": {"type": "boolean", "description": "Find the smallest counterexample", "default": True},
            "max_seconds": {"type": "number", "description": "Time budget for shrinking", "default": 30},
        },
        "required": ["db_path", "trusted_sql", "sql"],
    },
}, {
    "name": "check_sql",
    "description": (
        "Check a SQL query you wrote BEFORE trusting its answer, with no known-good query needed. Runs it "
        "read-only and checks every comparison of a column with a text literal (=, !=, IN, NOT IN, LIKE): "
        "if the database has no such value (e.g. you wrote status = 'Completed' but the data says 'completed', "
        "or bond_type = 'single' but the data uses '-'), it returns the closest real values. Also warns when "
        "the result is empty. Call it on every query you are about to answer from, and fix any finding."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "db_path": {"type": "string", "description": "SQLite or DuckDB file, Parquet/CSV/JSON file or folder, "
                                                         "or a postgresql:// URL"},
            "sql": {"type": "string", "description": "The query to check"},
        },
        "required": ["db_path", "sql"],
    },
}, {
    "name": "describe_values",
    "description": (
        "Look at the data before writing SQL. With no table: lists tables and their columns. With a table: its "
        "columns with sample values. With a table and column: the most common values with counts, the number of "
        "distinct values, NULLs, min and max. Use it to learn how values are actually written (codes, case, date "
        "formats) instead of guessing."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "db_path": {"type": "string", "description": "SQLite or DuckDB file, Parquet/CSV/JSON file or folder, "
                                                         "or a postgresql:// URL"},
            "table": {"type": "string", "description": "Table name (optional)"},
            "column": {"type": "string", "description": "Column name (optional, needs table)"},
            "limit": {"type": "integer", "description": "How many top values to return", "default": 20},
        },
        "required": ["db_path"],
    },
}]


def _with_con(db_path, fn):
    from .core import connect_readonly
    con = connect_readonly(db_path)
    try:
        return fn(con)
    finally:
        con.close()


def check_sql(db_path, sql):
    from . import probe
    out = _with_con(db_path, lambda con: probe.check_sql(con, sql))
    out["rows"] = _preview(out["columns"], out["rows"])["rows"] if out["rows"] is not None else None
    return out


def describe_values(db_path, table=None, column=None, limit=20):
    from . import probe
    return _with_con(db_path, lambda con: probe.describe_values(con, table, column, int(limit)))


HANDLERS = {"compare_queries": lambda a: compare_queries(**a), "check_sql": lambda a: check_sql(**a),
            "describe_values": lambda a: describe_values(**a)}


def _preview(cols, rows, limit=10):
    return {"columns": list(cols or []), "rows": [[str(v) if isinstance(v, bytes) else v for v in r]
                                                   for r in (rows or [])[:limit]],
            "total_rows": len(rows or [])}


def compare_queries(db_path, trusted_sql, sql, ordered=False, shrink=True, max_seconds=30):
    from . import blame as blame_mod
    from . import shrink as shrink_mod
    from .core import SILENT, connect_readonly, score
    con = connect_readonly(db_path)
    try:
        q = {"id": "q", "question": "", "gold_sql": trusted_sql, "ordered": bool(ordered)}
        r = score(con, [q], {"q": sql})[0]
    finally:
        con.close()
    out = {"same_result": r.verdict == "pass", "verdict": r.verdict, "detail": r.detail,
           "trusted_result": _preview(r.gold_cols, r.gold_rows), "result": _preview(r.agent_cols, r.agent_rows)}
    if shrink and r.verdict in SILENT:
        cx = shrink_mod.shrink(db_path, q, sql, max_seconds=float(max_seconds))
        if cx:
            b = blame_mod.explain(db_path, trusted_sql, sql, cx, bool(ordered))
            out["counterexample"] = {
                "rows": cx.rows, "source_rows": cx.source_rows, "minimal": cx.one_minimal,
                "tables": {t: _preview(c, rs, 20) for t, (c, rs) in cx.tables.items()},
                "trusted_result_on_these_rows": _preview(*cx.gold),
                "result_on_these_rows": _preview(*cx.agent),
                "recreate_sql": cx.to_sql(),
            }
            if b:
                out["cause"] = {"clause": b["clause"], "trusted_has": b["expected"], "checked_has": b["actual"],
                                "confirmed_on_full_database": b["full_db"]}
    return out


def _reply(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    msg["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def handle(msg):
    """Handle one JSON-RPC message; returns (id, result, error) or None for notifications."""
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if msg_id is None:
        return None  # notification, e.g. notifications/initialized
    if method == "initialize":
        asked = params.get("protocolVersion")
        return msg_id, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "goldcheck", "version": __version__}}, None
    if method == "ping":
        return msg_id, {}, None
    if method == "tools/list":
        return msg_id, {"tools": TOOLS}, None
    if method == "tools/call":
        name = params.get("name")
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            return msg_id, None, {"code": -32602, "message": f"Unknown tool: {name}"}
        args = params.get("arguments") or {}
        try:
            data = HANDLERS[name]({k: args[k] for k in args if k in tool["inputSchema"]["properties"]})
            return msg_id, {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}],
                            "isError": False}, None
        except Exception as exc:  # tool errors go back to the agent as results, per MCP
            return msg_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                            "isError": True}, None
    return msg_id, None, {"code": -32601, "message": f"Method not found: {method}"}


def serve(stdin=None):
    for line in (stdin or sys.stdin):
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _reply(None, error={"code": -32700, "message": "Parse error"})
            continue
        out = handle(msg)
        if out:
            _reply(*out)
