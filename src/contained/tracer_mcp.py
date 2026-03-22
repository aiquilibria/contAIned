#!/usr/bin/env python3
"""
Minimal MCP server for the contAIned tracer database.

Exposes two tools:
  get_schema   — return table DDL so Claude knows what's queryable
  query_tracer — run a read-only SQL query and return results

Usage:
  python3 tracer_mcp.py [--db /path/to/tracer.db]

Defaults to /workspace/.contAIned/tracer.db.
Communicates over stdin/stdout using JSON-RPC 2.0 (MCP wire protocol).
No third-party dependencies required.
"""

import json
import sqlite3
import sys
from pathlib import Path

# ── DB path ────────────────────────────────────────────────────────────────────

DB_PATH = "/workspace/.contAIned/tracer.db"
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--db" and i + 1 < len(args):
        DB_PATH = args[i + 1]
        break

# ── Tool definitions ───────────────────────────────────────────────────────────

_TOOLS = [
    {
        "name": "get_schema",
        "description": (
            "Return the contAIned tracer database schema. "
            "Call this first to understand what tables and columns are available "
            "before writing SQL queries. "
            "IMPORTANT: only call this tool when the user has explicitly invoked "
            "the /contained:tracer command. Do not call it for general questions."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "query_tracer",
        "description": (
            "Run a read-only SQL query against the contAIned tracer database. "
            "Tables: tasks (session history), audit_events (every tool call), "
            "snapshots (file write events), baselines (pre-task file state), "
            "blobs (content store). "
            "Only SELECT and WITH queries are permitted. Returns up to 500 rows. "
            "IMPORTANT: only call this tool when the user has explicitly invoked "
            "the /contained:tracer command. Do not call it for general questions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A read-only SQL query (SELECT or WITH ... SELECT).",
                }
            },
            "required": ["sql"],
        },
    },
]

# ── Tool implementations ───────────────────────────────────────────────────────


def _connect() -> sqlite3.Connection | str:
    """Return a connection or an error string."""
    if not Path(DB_PATH).exists():
        return f"Error: tracer.db not found at {DB_PATH}. Run `contained init` first."
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def tool_get_schema() -> str:
    conn = _connect()
    if isinstance(conn, str):
        return conn
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return "\n\n".join(f"-- {r['name']}\n{r['sql']}" for r in rows if r["sql"])
    except sqlite3.Error as exc:
        return f"Error: {exc}"
    finally:
        conn.close()


def tool_query_tracer(sql: str) -> str:
    conn = _connect()
    if isinstance(conn, str):
        return conn
    stripped = sql.strip().upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
        return "Error: only SELECT / WITH queries are allowed."
    try:
        rows = conn.execute(sql).fetchmany(500)
        if not rows:
            return "(no rows)"
        cols = list(rows[0].keys())
        header = " | ".join(cols)
        separator = "-" * len(header)
        data = "\n".join(" | ".join("" if v is None else str(v) for v in row) for row in rows)
        return f"{header}\n{separator}\n{data}"
    except sqlite3.Error as exc:
        return f"SQL error: {exc}"
    finally:
        conn.close()


# ── JSON-RPC dispatch ──────────────────────────────────────────────────────────


def _handle(req: dict) -> dict | None:
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params") or {}

    def ok(result: object) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code: int, msg: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tracer", "version": "0.1.0"},
            }
        )

    if method in ("notifications/initialized", "initialized"):
        return None  # notification — no response

    if method == "tools/list":
        return ok({"tools": _TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if name == "get_schema":
            text = tool_get_schema()
        elif name == "query_tracer":
            text = tool_query_tracer(arguments.get("sql", ""))
        else:
            return err(-32601, f"Unknown tool: {name}")
        return ok({"content": [{"type": "text", "text": text}]})

    if rid is not None:
        return err(-32601, f"Method not found: {method}")
    return None  # unknown notification — ignore


# ── Main loop ─────────────────────────────────────────────────────────────────


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        resp = _handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
