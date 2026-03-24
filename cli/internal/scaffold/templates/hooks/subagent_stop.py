#!/usr/bin/env python3
"""
SubagentStop hook — marks a sub-agent task as closed in tracer.db.

Fires when a sub-agent finishes (successfully or otherwise).  Transitions the
sub-agent's task from open → closed so the root summarizer can proceed.

Event fields used:
  agent_id — the sub-agent that just finished

This hook must never block execution (always exits 0).
"""
import json
import sys
from pathlib import Path

try:
    event = json.load(sys.stdin)
except (json.JSONDecodeError, EOFError):
    sys.exit(0)

cwd      = event.get("cwd", ".")
agent_id = event.get("agent_id")

if not agent_id:
    sys.exit(0)

db_path = str(Path(cwd) / ".contAIned" / "tracer.db")

try:
    from contained.tracer import contAInedTracer  # noqa: PLC0415
    tracer = contAInedTracer(db_path)
    tracer.set_task_status(agent_id, "closed")
except Exception:
    pass

sys.exit(0)
