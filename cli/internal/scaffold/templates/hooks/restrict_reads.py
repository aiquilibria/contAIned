#!/usr/bin/env python3
"""
PreToolUse hook — restricts Read, Glob, and Grep tool calls.

Policy is evaluated by the Cedar-inspired engine (contained.engine).
Falls back to _policy.py pattern matching if the engine is unavailable.

Outcomes:
  DENY     → JSON permissionDecision:deny  + exit 0
  ALLOW    → JSON permissionDecision:allow + exit 0
  ESCALATE → JSON permissionDecision:ask   + exit 0
  DEFER    → exit 0 (pass through to Claude Code's pipeline)
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


def _log(event: dict, target: str, policy_data: dict) -> None:
    """Append a structured audit entry to pipeline.jsonl. Never raises."""
    try:
        project_root = Path(event.get("cwd", "."))
        audit_log = project_root / ".contAIned" / "audit" / "pipeline.jsonl"
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "session_id": event.get("session_id"),
            "tool":       event.get("tool_name"),
            "input":      {"target": target},
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
            tool_input={"file_path": target},
            outcome=policy_data.get("outcome", "denied"),
            reason=policy_data.get("reason"),
        )
    except Exception:
        pass


_IMAGES_MOUNT   = "/workspace/.images/"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".pdf"}

try:
    event = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

tool       = event.get("tool_name", "")
tool_input = event.get("tool_input", {})

# ── /workspace/.images/ gate ──────────────────────────────────────────────────
# Allow image files; block everything else. This is enforced before the policy
# engine so it applies regardless of engine availability.
if tool in ("Read", "Glob", "Grep"):
    _target = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("pattern")
        or ""
    )
    if _target.startswith(_IMAGES_MOUNT):
        if Path(_target).suffix.lower() in _IMAGE_SUFFIXES:
            _allow()
        else:
            _deny(f"Only image files are accessible under {_IMAGES_MOUNT}")
        sys.exit(0)

if tool not in ("Read", "Glob", "Grep"):
    sys.exit(0)

# ── Engine path ───────────────────────────────────────────────────────────────
try:
    from contained.engine import (
        build_file_path_entity,
        build_glob_pattern_entity,
        evaluate,
        extract_file_targets,
        is_glob_tool,
        load_rules,
        load_secrets_patterns,
    )
    from contained.engine.entities import Outcome, build_agent_session, build_context

    principal = build_agent_session(event)
    context   = build_context(event)
    rules     = load_rules()
    patterns  = load_secrets_patterns()
    targets   = extract_file_targets(tool, tool_input)

    if not targets:
        sys.exit(0)

    glob = is_glob_tool(tool)

    for target in targets:
        if glob:
            entity = build_glob_pattern_entity(target)
        else:
            entity = build_file_path_entity(target, secrets_patterns=patterns)
        decision = evaluate(rules, tool, entity, principal, context=context)

        if decision.outcome == Outcome.DENY:
            _log(event, target, {
                "outcome":  "deny",
                "rule_id":  decision.rule_id,
                "reason":   decision.reason,
            })
            _deny(decision.reason or f"Access denied: {target}")
            sys.exit(0)

        if decision.outcome == Outcome.ALLOW:
            _allow()
            sys.exit(0)

        if decision.outcome == Outcome.ESCALATE:
            _ask()
            sys.exit(0)

        # DEFER — continue checking remaining targets

    sys.exit(0)

# ── Fallback: _policy.py pattern matching ─────────────────────────────────────
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from _policy import load_policy  # noqa: E402

cwd    = event.get("cwd", ".")
policy = load_policy(cwd)

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

for _action, _patterns, _reason in policy["secrets"]["_compiled_rules"]:
    if any(p.search(target) for p in _patterns):
        if _action == "allow":
            break
        _log(event, target, {"outcome": "deny", "reason": _reason})
        _deny(_reason or f"Access denied: secret files (credentials, keys, .env) may not be read.")
        sys.exit(0)

sys.exit(0)
