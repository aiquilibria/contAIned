#!/usr/bin/env python3
"""
PostCompact hook — records a context compaction event in tracer.db.

Fires after Claude Code automatically compacts the conversation context.
Compaction is an agent-affecting event (context is truncated) that is
invisible to the PostToolUse audit trail, so it is recorded here as an
OperatorShell-style audit event to preserve a complete timeline.

Event fields used:
  session_id        — current session
  hook_event_name   — always "PostCompact"
  trigger           — "auto" | "manual" (how compaction was triggered)
  preTokens         — token count before compaction
  postTokens        — token count after compaction (if available)

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
session_id = event.get("session_id")
agent_id   = event.get("agent_id")
actor_id   = agent_id or session_id

if not actor_id:
    sys.exit(0)

trigger     = event.get("trigger", "auto")
pre_tokens  = event.get("preTokens")
post_tokens = event.get("postTokens")

detail: dict = {"trigger": trigger}
if pre_tokens is not None:
    detail["preTokens"] = pre_tokens
if post_tokens is not None:
    detail["postTokens"] = post_tokens

db_path = str(Path(cwd) / ".contAIned" / "tracer.db")

try:
    from contained.tracer import contAInedTracer  # noqa: PLC0415

    tracer = contAInedTracer(db_path)
    tracer.log_event(
        session_id=actor_id,
        tool="ContextCompaction",
        tool_input=detail,
        outcome="success",
    )
except Exception:
    pass

sys.exit(0)
