#!/usr/bin/env python3
"""
PreToolUse hook — restricts Bash tool calls.

All checks are driven by manifest.yaml (baked into the container image).

Checks applied in order:
  1. policy.secrets.rules — read cmds targeting secret files (first match wins)
  2. policy.bash.rules    — ordered rule list; first match wins
     Each rule: {name, patterns, reason, action: allow|block|escalate}

For each command this hook outputs one of three JSON decisions:
  permissionDecision: "deny"  — blocked by a bash rule or secret-file check
  permissionDecision: "allow" — matches an action:allow rule in policy.bash.rules
  permissionDecision: "ask"   — no rule matched; operator prompt fires

The "allow" and "ask" paths exist because a PreToolUse hook returning "ask"
overrides the managed-settings allow list.  Safe commands must therefore be
listed first in policy.bash.rules with action:allow so they are not accidentally
caught by the "ask" fallback.

Outputs JSON to stdout; denials also written to the audit log.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _policy import load_policy  # noqa: E402


READ_CMD_RE = re.compile(
    r'^\s*(cat|head|tail|less|more|bat|pg|view|grep|egrep|fgrep|rg|ag|ack|sed|awk)\s',
    re.IGNORECASE,
)



def abs_path_tokens(command):
    """Extract tokens that resolve to absolute paths, including ~/... home-relative paths."""
    result = []
    for t in command.split():
        expanded = os.path.expanduser(t)
        if expanded.startswith("/") and len(expanded) > 1:
            result.append(expanded)
    return result


def log_denial(event, command, reason):
    try:
        project_root = Path(event.get("cwd", "."))
        audit_log = project_root / ".contAIned" / "audit" / "pipeline.jsonl"
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "session_id": event.get("session_id"),
            "tool":       event.get("tool_name"),
            "input":      {"command": command},
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
            tool_input={"command": command},
            outcome="denied",
            reason=reason,
        )
    except Exception:
        pass


def enforce(action, event, command, msg):
    """Block the tool call: log denial, output JSON deny decision, exit 2."""
    if action == "block":
        log_denial(event, command, msg)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": msg,
            }
        }))
        sys.exit(2)


def _ask():
    """Output a permissionDecision:ask response and exit 0."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
        }
    }))
    sys.exit(0)


try:
    event = json.load(sys.stdin)
except json.JSONDecodeError:
    _ask()

command = event.get("tool_input", {}).get("command", "")
if not command:
    _ask()

cwd    = event.get("cwd", ".")
policy = load_policy(cwd)

# ── Check 1: secret file reads ────────────────────────────────────────────────
_sec_rules = policy["secrets"]["_compiled_rules"]
if READ_CMD_RE.match(command):
    for token in command.split():
        for _action, _patterns, _reason in _sec_rules:
            if any(p.search(token) for p in _patterns):
                if _action == "allow":
                    break  # safe variant token — skip
                enforce(
                    _action, event, command,
                    _reason or f"Bash denied: \'{token}\' looks like a secret file.",
                )
                break  # first matching rule for this token wins

# ── Bash command rules (allow / block / escalate) ────────────────────────────
for _action, _patterns, _reason in policy["bash"]["_compiled_rules"]:
    if any(pat.search(command) for pat in _patterns):
        if _action == "allow":
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }))
            sys.exit(0)
        elif _action == "block":
            enforce(_action, event, command, _reason)
        else:  # escalate
            _ask()

# No rule matched — ask the operator for explicit approval.
_ask()
