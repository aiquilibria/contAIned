#!/usr/bin/env python3
"""
PreToolUse hook — restricts Read, Glob, and Grep tool calls.

Checks are driven by the policy: section of .contAIned/manifest.yaml.
  policy.secrets.reads         — action for secret-file read attempts
  policy.secrets.safe_variants — action for .env.example / template variants

Workspace boundary is enforced by the Docker container at the kernel level.

Action values: block | allow | escalate
  block    — deny (exit 2, reason on stderr)
  allow    — permit unconditionally; skip the check
  escalate — pass through (exit 0); SDK's canUseTool callback decides

Exits 0 to allow, 2 to deny (reason on stderr fed back to agent).
Denials are written to the audit log before blocking.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _policy import load_policy  # noqa: E402


def log_denial(event, target, reason):
    """Append a denial entry to the audit log. Never raises."""
    try:
        project_root = Path(event.get("cwd", "."))
        audit_log = project_root / ".contAIned" / "audit" / "pipeline.jsonl"
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "session_id": event.get("session_id"),
            "tool":       event.get("tool_name"),
            "input":      {"target": target},
            "outcome":    "denied",
            "reason":     reason,
        }
        with audit_log.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    # Mirror denial into tracer.db so it is queryable alongside allowed events.
    try:
        from contained.tracer import contAInedTracer  # noqa: PLC0415
        _db = str(Path(event.get("cwd", ".")) / ".contAIned" / "tracer.db")
        contAInedTracer(_db).log_event(
            session_id=event.get("agent_id") or event.get("session_id"),
            tool=event.get("tool_name", ""),
            tool_input={"file_path": target},
            outcome="denied",
            reason=reason,
        )
    except Exception:
        pass


def enforce(action, event, target, msg):
    """Act on *action*: block exits 2; allow/escalate pass through."""
    if action == "block":
        log_denial(event, target, msg)
        print(msg, file=sys.stderr)
        sys.exit(2)


try:
    event = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

cwd        = event.get("cwd", ".")
policy     = load_policy(cwd)
tool       = event.get("tool_name", "")
tool_input = event.get("tool_input", {})

if tool == "Read":
    target = tool_input.get("file_path", "")
elif tool == "Grep":
    target = tool_input.get("path", "")
elif tool == "Glob":
    target = tool_input.get("pattern", "")
else:
    sys.exit(0)

if not target:
    sys.exit(0)

# ── Secret file ───────────────────────────────────────────────────────────────
for _action, _patterns, _reason in policy["secrets"]["_compiled_rules"]:
    if any(p.search(target) for p in _patterns):
        if _action == "allow":
            break  # safe variant — permit
        enforce(
            _action, event, target,
            _reason or "Access denied: secret files (credentials, keys, .env) may not be read.",
        )
        break

sys.exit(0)
