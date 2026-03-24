#!/usr/bin/env python3
"""
PreToolUse hook — captures file baselines before Write, Edit, or MultiEdit.

Runs after restrict_writes.py in the PreToolUse chain.  When restrict_writes.py
exits 2 (deny), the SDK aborts the chain and this hook never fires — no baseline
is recorded for denied writes.  This is correct behaviour.

Actor ID resolution:
  actor_id = agent_id or session_id   (agent_id present only for sub-agent calls)

This hook must never block writes (always exits 0).
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

tool_input = event.get("tool_input") or {}
cwd        = event.get("cwd", ".")

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

# ── Capture baselines ──────────────────────────────────────────────────────────
db_path = str(Path(cwd) / ".contAIned" / "tracer.db")

try:
    from contained.tracer import contAInedTracer  # noqa: PLC0415
    tracer = contAInedTracer(db_path)
    for file_path in file_paths:
        tracer.capture_baseline(actor_id, file_path)
except Exception:
    pass  # tracer must never block writes

sys.exit(0)
