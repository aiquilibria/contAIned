#!/usr/bin/env python3
"""
PreToolUse hook — restricts Bash tool calls.

Policy is evaluated by the Cedar-inspired engine (contained.engine).
Falls back to _policy.py pattern matching if the engine is unavailable.

For each command this hook outputs one of three JSON decisions:
  permissionDecision: "deny"  — blocked by the engine (DENY outcome)
  permissionDecision: "allow" — engine returned ALLOW (explicit permit rule)
  permissionDecision: "ask"   — engine returned ESCALATE or DEFER (operator prompt)

The "allow" and "ask" paths exist because a PreToolUse hook returning "ask"
overrides the managed-settings allow list.  Safe commands must be permitted
by a manifest rule (effect: permit) or they will trigger the "ask" fallback.

All outcomes exit 0; denial reason is embedded in the JSON.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def _allow() -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }))


def _ask() -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
        }
    }))


def _log(event: dict, command: str, policy_data: dict) -> None:
    """Append a structured audit entry to pipeline.jsonl. Never raises."""
    try:
        project_root = Path(event.get("cwd", "."))
        audit_log = project_root / ".contAIned" / "audit" / "pipeline.jsonl"
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "session_id": event.get("session_id"),
            "tool":       event.get("tool_name"),
            "input":      {"command": command},
            "policy":     policy_data,
        }
        with audit_log.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    try:
        from contained.tracer import contAInedTracer  # noqa: PLC0415
        _db = str(Path(event.get("cwd", ".")) / ".contAIned" / "tracer.db")
        contAInedTracer(_db).log_event(
            session_id=event.get("agent_id") or event.get("session_id"),
            tool=event.get("tool_name", ""),
            tool_input={"command": command},
            outcome=policy_data.get("outcome", "denied"),
            reason=policy_data.get("reason"),
        )
    except Exception:
        pass


try:
    event = json.load(sys.stdin)
except json.JSONDecodeError:
    _ask()
    sys.exit(0)

command = event.get("tool_input", {}).get("command", "")
if not command:
    _ask()
    sys.exit(0)

# ── Engine path ───────────────────────────────────────────────────────────────
try:
    from contained.engine import (
        build_bash_command_entity,
        evaluate,
        load_rules,
        load_secrets_patterns,
    )
    from contained.engine.entities import Outcome, build_agent_session, build_context

    principal = build_agent_session(event)
    context   = build_context(event)
    rules     = load_rules()
    patterns  = load_secrets_patterns()
    entity    = build_bash_command_entity(command, secrets_patterns=patterns)
    decision  = evaluate(rules, "Bash", entity, principal, context=context)

    if decision.outcome == Outcome.DENY:
        _log(event, command, {
            "outcome": "deny",
            "rule_id": decision.rule_id,
            "reason":  decision.reason,
        })
        _deny(decision.reason or f"Bash command denied: {command}")
        sys.exit(0)

    if decision.outcome == Outcome.ALLOW:
        _allow()
        sys.exit(0)

    # ESCALATE or DEFER → ask operator
    _ask()
    sys.exit(0)

# ── Fallback: _policy.py pattern matching ─────────────────────────────────────
except ImportError:
    pass

import re

sys.path.insert(0, str(Path(__file__).parent))
from _policy import load_policy  # noqa: E402

cwd    = event.get("cwd", ".")
policy = load_policy(cwd)

READ_CMD_RE = re.compile(
    r'^\s*(cat|head|tail|less|more|bat|pg|view|grep|egrep|fgrep|rg|ag|ack|sed|awk)\s',
    re.IGNORECASE,
)

_sec_rules = policy["secrets"]["_compiled_rules"]
if READ_CMD_RE.match(command):
    for token in command.split():
        for _action, _patterns, _reason in _sec_rules:
            if any(p.search(token) for p in _patterns):
                if _action == "allow":
                    break
                _log(event, command, {"outcome": "deny", "reason": _reason})
                _deny(_reason or f"Bash denied: '{token}' looks like a secret file.")
                sys.exit(0)

for _action, _patterns, _reason in policy["bash"]["_compiled_rules"]:
    if any(pat.search(command) for pat in _patterns):
        if _action == "allow":
            _allow()
            sys.exit(0)
        elif _action == "block":
            _log(event, command, {"outcome": "deny", "reason": _reason})
            _deny(_reason or f"Bash command denied: {command}")
            sys.exit(0)
        else:
            _ask()
            sys.exit(0)

_ask()
sys.exit(0)
