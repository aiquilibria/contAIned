#!/usr/bin/env python3
"""
PostToolUse hook — detects successful git push Bash commands and logs a GitPush
audit event so the stop hook's push-processing path can detect it reliably.

The actual payload assembly (build_actions, assemble_payload, POST) happens in
the stop hook (summarizer.py) which has the full session transcript available.

This hook must never block execution (always exits 0).
"""
import json
import re
import sys

try:
    event = json.load(sys.stdin)
except (json.JSONDecodeError, EOFError):
    sys.exit(0)

tool = event.get("tool_name", "")
if tool != "Bash":
    sys.exit(0)

tool_input    = event.get("tool_input") or {}
tool_response = event.get("tool_response") or {}

cmd = (tool_input.get("command") or "").strip()
if not (re.match(r'^git\s+push\b', cmd) and "--dry-run" not in cmd):
    sys.exit(0)

exit_code = tool_response.get("exit_code")
if exit_code is not None and exit_code != 0:
    sys.exit(0)

session_id = event.get("session_id")
agent_id   = event.get("agent_id")
actor_id   = agent_id or session_id
cwd        = event.get("cwd", ".")

if not actor_id:
    sys.exit(0)

try:
    from pathlib import Path
    from contained.tracer import contAInedTracer  # noqa: PLC0415
    db_path = str(Path(cwd) / ".contAIned" / "tracer.db")
    tracer = contAInedTracer(db_path)
    tracer.log_event(
        session_id=actor_id,
        tool="GitPush",
        tool_input={"command": cmd},
        outcome="success",
        reason=None,
    )
except Exception:
    pass

sys.exit(0)
