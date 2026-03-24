#!/usr/bin/env python3
"""
PreToolUse hook — restricts Write, Edit, MultiEdit.

Checks in order:
  1. Control-plane protection  — .contAIned/ writes are ALWAYS denied (not configurable)
  2. Settings protection       — .claude/settings.json is ALWAYS denied (not configurable)
  3. Secret file               — driven by policy.secrets.writes

Workspace boundary is enforced by the Docker container at the kernel level.

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
    """Append a denial entry to the audit log. Never raises — audit must not block."""
    try:
        project_root = Path(event.get("cwd", "."))
        audit_log = project_root / ".contAIned" / "audit" / "pipeline.jsonl"
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "session_id": event.get("session_id"),
            "tool":       event.get("tool_name"),
            "input":      {"file_path": target},
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
    sys.exit(0)  # malformed input — pass through, don't block

tool       = event.get("tool_name", "")
tool_input = event.get("tool_input", {})

if tool not in ("Write", "Edit", "MultiEdit"):
    sys.exit(0)

target = tool_input.get("file_path", "")
if not target:
    sys.exit(0)

cwd          = event.get("cwd", ".")
policy       = load_policy(cwd)
project_root = Path(cwd).resolve()
contAIned_dir    = project_root / ".contAIned"
resolved     = Path(target).resolve()

# ── Check 1: inside .contAIned/ control-plane (always enforced, not configurable) ─
try:
    resolved.relative_to(contAIned_dir)
    log_denial(event, target, f"write into control-plane directory: {target}")
    print(
        f"Write denied: \'{target}\' is inside the .contAIned/ control-plane directory.\n"
        "Hook and policy files are managed by contAIned and must not be edited directly.",
        file=sys.stderr,
    )
    sys.exit(2)
except ValueError:
    pass  # not inside .contAIned/ — continue

# ── Check 2: .claude/settings.json — Claude Code hook registration file ───────
claude_settings = (project_root / ".claude" / "settings.json").resolve()
if resolved == claude_settings:
    log_denial(event, target, f"write to Claude Code settings file: {target}")
    print(
        f"Write denied: \'{target}\' is the Claude Code settings file.\n"
        "Hook registration is managed by contAIned and must not be edited directly.",
        file=sys.stderr,
    )
    sys.exit(2)

# ── Check 3: secret file ───────────────────────────────────────────────────────
for _action, _patterns, _reason in policy["secrets"]["_compiled_rules"]:
    if any(p.search(str(resolved)) for p in _patterns):
        if _action == "allow":
            break  # safe variant — permit
        enforce(
            _action, event, target,
            _reason or f"Write denied: \'{target}\' looks like a secret file.",
        )
        break

sys.exit(0)
