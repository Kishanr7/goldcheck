"""An MCP server over stdio, standard library only: `goldcheck mcp`.

Three tools let a chatbot or coding agent check its own SQL before it answers:
check_sql (no gold answer needed), describe_values (look at real values first) and
compare_queries (against a trusted query, with the smallest counterexample and the clause
that causes the difference).

Databases are configured when the server starts, never sent by the model:

    goldcheck mcp --db sales=postgresql://reader@db.internal/sales --db shop=./shop.sqlite

The model only sees and sends the names ("sales", "shop"), so connection strings and
passwords never enter its context. Without --db (or GOLDCHECK_DB), the tools take a
`db_path` argument instead, which suits local use on your own files. `--allow-paths`
permits both.

Messages are newline-delimited JSON-RPC 2.0 in UTF-8, as the MCP stdio transport specifies.
"""
from __future__ import annotations

import json
import os
import sys

from . import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

CONFIG = {"databases": {}, "allow_paths": True}

_DB_PATH = {"type": "string", "description": "SQLite or DuckDB file, Parquet/CSV/JSON file or folder, "
                                             "or a postgresql:// URL"}

_TOOLS = [{
    "name": "check_sql",
    "description": (
        "Check a SQL query you wrote BEFORE trusting its answer, with no known-good query needed. Runs it "
        "read-only and checks every comparison of a column with a text literal (=, !=, IN, NOT IN, LIKE): "
        "if the database has no such value (e.g. you wrote status = 'Completed' but the data says 'completed', "
        "or bond_type = 'single' but the data uses '-'), it returns the closest real values. Also warns when "
        "the result is empty. Call it on every query you are about to answer from, and fix any error finding."),
    "properties": {"sql": {"type": "string", "description": "The query to check"}},
    "required": ["sql"],
}, {
    "name": "describe_values",
    "description": (
        "Look at the data before writing SQL. With no table: lists tables and their columns. With a table: its "
        "columns with sample values. With a table and column: the most common values with counts, the number of "
        "distinct values, NULLs, min and max. Use it to learn how values are actually written (codes, case, date "
        "formats) instead of guessing."),
    "properties": {
        "table": {"type": "string", "description": "Table name (optional)"},
        "column": {"type": "string", "description": "Column name (optional, needs table)"},
        "limit": {"type": "integer", "description": "How many top values to return", "default": 20},
    },
    "required": [],
}, {
    "name": "compare_queries",
    "description": (
        "Run a trusted SQL query and a query to check on the same database (read-only) and report whether they "
        "return the same result. If they differ silently, also return the smallest set of real rows on which "
        "they still differ, the clause of the checked query that causes it, and a SQL script that recreates "
        "those rows. Use it to verify SQL you rewrote or refactored when the original is known to be right."),
    "properties": {
        "trusted_sql": {"type": "string", "description": "The query whose result is known to be right"},
        "sql": {"type": "string", "description": "The query to check"},
        "ordered": {"type": "boolean", "description": "Row order matters", "default": False},
        "shrink": {"type": "boolean", "description": "Find the smallest counterexample", "default": True},
        "max_seconds": {"type": "number", "description": "Time budget for shrinking", "default": 30},
    },
    "required": ["trusted_sql", "sql"],
}]


def configure(databases=None, allow_paths=None):
    """Set the databases this server serves, as {name: path or URL}."""
    CONFIG["databases"] = dict(databases or {})
    CONFIG["allow_paths"] = (not CONFIG["databases"]) if allow_paths is None else bool(allow_paths)


def parse_db_args(values, env=None):
    """--db NAME=TARGET (repeatable) plus GOLDCHECK_DB=TARGET (named "default")."""
    dbs = {}
    for item in values or []:
        name, sep, target = item.partition("=")
        if not sep or not name.strip() or not target.strip():
            raise ValueError(f"--db expects NAME=PATH_OR_URL, got {item.split('=')[0]!r}...")
        dbs[name.strip()] = target.strip()
    env_target = (env if env is not None else os.environ).get("GOLDCHECK_DB")
    if env_target and "default" not in dbs:
        dbs["default"] = env_target
    return dbs


def tool_list():
    names = sorted(CONFIG["databases"])
    tools = []
    for t in _TOOLS:
        props, required = {}, []
        if names:
            props["database"] = {"type": "string", "enum": names,
                                 "description": "Which configured database to use: " + ", ".join(names)}
            if len(names) > 1:
                required.append("database")
        if CONFIG["allow_paths"]:
            props["db_path"] = _DB_PATH
            if not names:
                required.append("db_path")
        props.update(t["properties"])
        tools.append({"name": t["name"], "description": t["description"],
                      "inputSchema": {"type": "object", "properties": props, "required": required + t["required"]}})
    return tools


class ToolError(Exception):
    """A problem with the call that the agent can fix; the message is shown to it."""


def _target(args):
    names = CONFIG["databases"]
    if args.get("db_path") is not None:
        if not CONFIG["allow_paths"]:
            raise ToolError("this server only serves its configured databases: " + ", ".join(sorted(names))
                            + ". Pass `database` instead of `db_path`.")
        return str(args["db_path"])
    if not names:
        raise ToolError("missing `db_path`")
    name = args.get("database")
    if name is None:
        if len(names) == 1:
            return next(iter(names.values()))
        raise ToolError("missing `database`; choose one of: " + ", ".join(sorted(names)))
    if name not in names:
        raise ToolError(f"unknown database {name!r}; choose one of: " + ", ".join(sorted(names)))
    return names[name]


def _with_con(target, fn):
    from .core import connect_readonly
    con = connect_readonly(target)
    try:
        return fn(con)
    finally:
        con.close()


def _preview(cols, rows, limit=10):
    return {"columns": list(cols or []), "rows": [[str(v) if isinstance(v, (bytes, memoryview)) else v for v in r]
                                                   for r in (rows or [])[:limit]],
            "total_rows": len(rows or [])}


def check_sql(target, sql):
    from . import probe
    out = _with_con(target, lambda con: probe.check_sql(con, sql))
    preview = _preview(out["columns"], out["rows"]) if out["rows"] is not None else None
    out["rows"] = preview["rows"] if preview else None
    return out


def describe_values(target, table=None, column=None, limit=20):
    from . import probe
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        raise ToolError("`limit` must be a whole number") from None
    if column and not table:
        raise ToolError("`column` needs `table`")
    return _with_con(target, lambda con: probe.describe_values(con, table, column, limit))


def compare_queries(target, trusted_sql, sql, ordered=False, shrink=True, max_seconds=30):
    from . import blame as blame_mod
    from . import shrink as shrink_mod
    from .core import SILENT, connect_readonly, score
    try:
        max_seconds = max(1.0, min(float(max_seconds), 600.0))
    except (TypeError, ValueError):
        raise ToolError("`max_seconds` must be a number") from None
    con = connect_readonly(target)
    try:
        q = {"id": "q", "question": "", "gold_sql": trusted_sql, "ordered": bool(ordered)}
        r = score(con, [q], {"q": sql})[0]
    finally:
        con.close()
    out = {"same_result": r.verdict == "pass", "verdict": r.verdict, "detail": r.detail,
           "trusted_result": _preview(r.gold_cols, r.gold_rows), "result": _preview(r.agent_cols, r.agent_rows)}
    if shrink and r.verdict in SILENT:
        cx = shrink_mod.shrink(target, q, sql, max_seconds=max_seconds)
        if cx:
            b = blame_mod.explain(target, trusted_sql, sql, cx, bool(ordered))
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


HANDLERS = {
    "check_sql": lambda t, a: check_sql(t, a["sql"]),
    "describe_values": lambda t, a: describe_values(t, a.get("table"), a.get("column"), a.get("limit", 20)),
    "compare_queries": lambda t, a: compare_queries(t, a["trusted_sql"], a["sql"], a.get("ordered", False),
                                                    a.get("shrink", True), a.get("max_seconds", 30)),
}


def _call(name, args):
    tool = next((t for t in _TOOLS if t["name"] == name), None)
    if tool is None:
        return None
    if not isinstance(args, dict):
        raise ToolError("`arguments` must be an object")
    missing = [k for k in tool["required"] if args.get(k) in (None, "")]
    if missing:
        raise ToolError("missing " + ", ".join(f"`{k}`" for k in missing))
    for k in ("sql", "trusted_sql", "table", "column", "database", "db_path"):
        if k in args and args[k] is not None and not isinstance(args[k], str):
            raise ToolError(f"`{k}` must be a string")
    return HANDLERS[name](_target(args), args)


def handle(msg):
    """Handle one JSON-RPC message; returns (id, result, error) or None for notifications."""
    if not isinstance(msg, dict):
        return None, None, {"code": -32600, "message": "Invalid request: expected a JSON object"}
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if msg_id is None:
        return None  # notification, e.g. notifications/initialized
    if not isinstance(params, dict):
        return msg_id, None, {"code": -32602, "message": "Invalid params: expected an object"}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return msg_id, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "goldcheck", "version": __version__}}, None
    if method == "ping":
        return msg_id, {}, None
    if method == "tools/list":
        return msg_id, {"tools": tool_list()}, None
    if method == "tools/call":
        name = params.get("name")
        if not any(t["name"] == name for t in _TOOLS):
            return msg_id, None, {"code": -32602, "message": f"Unknown tool: {name}"}
        try:
            data = _call(name, params.get("arguments") or {})
            return msg_id, {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}],
                            "isError": False}, None
        except Exception as exc:  # tool errors go back to the agent as results, per MCP
            text = str(exc) if isinstance(exc, ToolError) else f"{type(exc).__name__}: {exc}"
            return msg_id, {"content": [{"type": "text", "text": text}], "isError": True}, None
    return msg_id, None, {"code": -32601, "message": f"Method not found: {method}"}


def _reply(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    msg["error" if error else "result"] = error or result
    data = (json.dumps(msg, default=str) + "\n").encode("utf-8")
    out = getattr(sys.stdout, "buffer", None)
    if out is None:  # e.g. a test replaced sys.stdout with a text stream
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()
    else:
        sys.stdout.flush()
        out.write(data)
        out.flush()


def serve(stdin=None):
    # MCP stdio is UTF-8 by specification, whatever the console code page (cp1252 on many
    # Windows machines). Read and write bytes as UTF-8 so non-English SQL and values survive.
    if stdin is None:
        stdin = (raw.decode("utf-8", errors="replace") for raw in sys.stdin.buffer)
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _reply(None, error={"code": -32700, "message": "Parse error"})
            continue
        try:
            out = handle(msg)
        except Exception as exc:  # never let one bad message stop the server
            out = (msg.get("id") if isinstance(msg, dict) else None, None,
                   {"code": -32603, "message": f"Internal error: {type(exc).__name__}"})
        if out:
            _reply(*out)
