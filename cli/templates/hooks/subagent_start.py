#!/usr/bin/env python3
"""
SubagentStart hook — registers a new sub-agent task in tracer.db.

Fires when a sub-agent is spawned via the Agent tool.  Records the sub-agent
as an open task, linked to the root session via parent_session_id.

Event fields used:
  agent_id   — the sub-agent's unique identifier  (actor for this task)
  session_id — always the root session's ID        (parent reference)

This hook must never block execution (always exits 0).
"""
import json
import sys
from pathlib import Path

try:
    event = json.load(sys.stdin)
except (json.JSONDecodeError, EOFError):
    sys.exit(0)

cwd        = event.get("cwd", ".")
session_id = event.get("session_id")   # root session (parent)
agent_id   = event.get("agent_id")     # sub-agent being spawned

if not agent_id or not session_id:
    sys.exit(0)

# Derive a human-readable prompt from whatever context the event provides.
agent_type  = event.get("agent_type") or event.get("subagent_type") or "sub-agent"
description = event.get("description") or event.get("prompt") or ""
prompt = ("[" + agent_type + "] " + description).strip() if description else ("[" + agent_type + "]")

db_path = str(Path(cwd) / ".contAIned" / "tracer.db")

try:
    from contained.tracer import contAInedTracer  # noqa: PLC0415
    tracer = contAInedTracer(db_path)
    tracer.open_task(agent_id, prompt, parent_session_id=session_id)
except Exception:
    pass

sys.exit(0)
