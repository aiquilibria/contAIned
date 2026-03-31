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

# Match git push in any of these forms:
#   git push ...
#   git -C <dir> push ...
#   <anything> && git push ...   (compound commands)
_GIT_PUSH_RE = re.compile(
    r'(?:^|&&|;|\|\|)\s*git(?:\s+-C\s+\S+)?\s+push\b'
)
if not (_GIT_PUSH_RE.search(cmd) and "--dry-run" not in cmd):
    sys.exit(0)

is_error  = tool_response.get("is_error", False)
exit_code = tool_response.get("exit_code")
failed    = is_error or (exit_code is not None and exit_code != 0)

session_id = event.get("session_id")
agent_id   = event.get("agent_id")
actor_id   = agent_id or session_id
cwd        = event.get("cwd", ".")

if not actor_id:
    sys.exit(0)

outcome = "denied" if failed else "success"
reason: str | None = None
if failed:
    content = tool_response.get("content")
    if isinstance(content, list):
        reason = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        ).strip() or None
    elif isinstance(content, str):
        reason = content or None

try:
    from pathlib import Path
    from contained.tracer import contAInedTracer  # noqa: PLC0415
    db_path = str(Path(cwd) / ".contAIned" / "tracer.db")
    tracer = contAInedTracer(db_path)
    tracer.log_event(
        session_id=actor_id,
        tool="GitPush",
        tool_input={"command": cmd},
        outcome=outcome,
        reason=reason,
    )
except Exception:
    pass

sys.exit(0)
