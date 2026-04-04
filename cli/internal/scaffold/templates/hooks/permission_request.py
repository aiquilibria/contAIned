#!/usr/bin/env python3
"""
PermissionRequest hook — audits every permission request to tracer.db.

Records the fact that the agent requested permission for a tool call.
Never blocks execution (always exits 0 without output).

To infer the user's decision, cross-reference with PostToolUse:
  - PermissionRequest entry + matching PostToolUse success  → user approved
  - PermissionRequest entry + no matching PostToolUse ~30s  → user denied (inferred)

Matching key: (session_id, tool_name, tool_input) + temporal proximity.
"""
import json
import sys
from pathlib import Path

try:
    event = json.load(sys.stdin)
except (json.JSONDecodeError, EOFError):
    sys.exit(0)

cwd        = event.get("cwd", ".")
session_id = event.get("session_id")
agent_id   = event.get("agent_id")
actor_id   = agent_id or session_id
tool       = event.get("tool_name", "")
tool_input = event.get("tool_input") or {}

try:
    from contained.tracer import contAInedTracer  # noqa: PLC0415
    db_path = str(Path(cwd) / ".contAIned" / "tracer.db")
    tracer  = contAInedTracer(db_path)
    tracer.log_event(
        session_id    = actor_id,
        tool          = tool,
        tool_input    = tool_input,
        outcome       = "permission_requested",
        reason        = None,
        tool_response = {},
    )
except Exception:
    pass  # never block execution due to logging failure

sys.exit(0)
