#!/usr/bin/env python3
"""
Minimal MCP server for the contAIned tracer database.

Exposes four tools:
  get_schema       — return table DDL so Claude knows what's queryable
  query_tracer     — run a read-only SQL query and return results
  list_work_units  — list work units with status, branch, and prompt
  get_payload      — assemble and return the ATP payload for a work unit

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
            "the contained:tracer skill. Do not call it for general questions."
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
            "the contained:tracer skill. Do not call it for general questions."
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
    {
        "name": "list_work_units",
        "description": (
            "List contAIned work units from the tracer database. "
            "Returns id, status, branch, base_commit, head_commit, opened_at, and prompt "
            "for each unit, newest first. "
            "IMPORTANT: only call this tool when the user has explicitly invoked "
            "the contained:submit skill."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: open, pushed, or abandoned. Omit for all.",
                }
            },
        },
    },
    {
        "name": "get_payload",
        "description": (
            "Assemble and return the work unit payload for a given work_unit_id. "
            "The payload contains the full invocation and outcome records. "
            "IMPORTANT: only call this tool when the user has explicitly invoked "
            "the contained:submit skill."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_unit_id": {
                    "type": "string",
                    "description": "The work unit UUID (full or unambiguous prefix).",
                }
            },
            "required": ["work_unit_id"],
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


def tool_list_work_units(status: str | None = None) -> str:
    conn = _connect()
    if isinstance(conn, str):
        return conn
    try:
        where = f"WHERE status = '{status}'" if status else ""
        rows = conn.execute(
            f"""
            SELECT id, status, branch, base_commit, head_commit, opened_at, prompt
            FROM work_units
            {where}
            ORDER BY opened_at DESC
            LIMIT 50
            """
        ).fetchall()
        if not rows:
            return "(no work units found)"
        lines = []
        for r in rows:
            head = (r["head_commit"] or "")[:8] or "(open)"
            base = (r["head_commit"] and r["base_commit"] or r["base_commit"] or "")[:8]
            prompt_short = (r["prompt"] or "")[:60]
            lines.append(
                f"[{r['status']}] {r['id'][:8]}…  {r['branch']}  {base}→{head}  {prompt_short}"
            )
        return "\n".join(lines)
    except sqlite3.Error as exc:
        return f"Error: {exc}"
    finally:
        conn.close()


def tool_get_payload(work_unit_id: str) -> str:
    if not Path(DB_PATH).exists():
        return f"Error: tracer.db not found at {DB_PATH}."
    try:
        from contained.tracer import contAInedTracer  # noqa: PLC0415

        tracer = contAInedTracer(DB_PATH)
        # Support unambiguous prefix matching.
        if len(work_unit_id) < 36:
            row = tracer.conn.execute(
                "SELECT id FROM work_units WHERE id LIKE ?",
                (f"{work_unit_id}%",),
            ).fetchone()
            if not row:
                return f"Error: no work unit found with prefix '{work_unit_id}'."
            work_unit_id = row[0]
        payload = tracer.assemble_payload(work_unit_id)
        return json.dumps(payload, indent=2, ensure_ascii=False)
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error assembling payload: {exc}"


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
        elif name == "list_work_units":
            text = tool_list_work_units(arguments.get("status"))
        elif name == "get_payload":
            text = tool_get_payload(arguments.get("work_unit_id", ""))
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
