#!/usr/bin/env python3
"""
PostToolUse hook — records file snapshots after Write, Edit, or MultiEdit.

Reads each affected file from disk (not from tool_input) so the stored blob
always matches what is actually on disk.  Handles Write, Edit, and MultiEdit
uniformly by resolving file paths from tool_input.

Actor ID resolution:
  actor_id = agent_id or session_id

This hook must never block execution (always exits 0).
"""
import json
import sys
from pathlib import Path

try:
    event = json.load(sys.stdin)
except (json.JSONDecodeError, EOFError):
    sys.exit(0)

tool = event.get("tool_name", "")
if tool not in ("Write", "Edit", "MultiEdit"):
    sys.exit(0)

tool_input    = event.get("tool_input") or {}
tool_response = event.get("tool_response") or {}
cwd           = event.get("cwd", ".")

# Skip if the write failed (is_error means the tool errored out)
if tool_response.get("is_error"):
    sys.exit(0)

# ── Actor ID resolution ────────────────────────────────────────────────────────
session_id = event.get("session_id")
agent_id   = event.get("agent_id")
actor_id   = agent_id or session_id

if not actor_id:
    sys.exit(0)

# ── Collect file paths ─────────────────────────────────────────────────────────
if tool == "MultiEdit":
    edits      = tool_input.get("edits") or []
    file_paths = list({e["file_path"] for e in edits if e.get("file_path")})
else:
    fp         = tool_input.get("file_path")
    file_paths = [fp] if fp else []

if not file_paths:
    sys.exit(0)

# ── Record snapshots ───────────────────────────────────────────────────────────
db_path = str(Path(cwd) / ".contAIned" / "tracer.db")

try:
    from contained.tracer import contAInedTracer  # noqa: PLC0415
    tracer = contAInedTracer(db_path)
    for file_path in file_paths:
        path = Path(file_path)
        if path.exists():
            content = path.read_bytes()
            tracer.track_write(actor_id, file_path, content)
except Exception:
    pass  # tracer must never block execution

sys.exit(0)
