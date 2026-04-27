#!/usr/bin/env python3
"""
PreToolUse hook — restricts Write, Edit, MultiEdit.

Structural pre-checks (always enforced, not configurable):
  1. Control-plane protection — .contAIned/ writes are always denied.
  2. Settings protection      — .claude/settings.json is always denied.

Policy checks (engine-driven):
  3. Secret-file protection   — from policy.secrets.rules via contained.engine.

MultiEdit is dispatched per target: any denied target denies the whole call.
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
            "input":      {"file_path": target},
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


try:
    event = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

tool       = event.get("tool_name", "")
tool_input = event.get("tool_input", {})

if tool not in ("Write", "Edit", "MultiEdit"):
    sys.exit(0)

cwd          = event.get("cwd", ".")
project_root = Path(cwd).resolve()
contAIned_dir = project_root / ".contAIned"

# ── Structural pre-check 1: .contAIned/ control-plane (always enforced) ───────
# Resolve the primary target for structural pre-checks (file_path for Write/Edit;
# first target for MultiEdit).
_primary = (
    tool_input.get("file_path", "")
    if tool in ("Write", "Edit")
    else (tool_input.get("edits") or [{}])[0].get("file_path", "")
)

if _primary:
    resolved = Path(_primary).resolve()
    _in_cp = False
    try:
        resolved.relative_to(contAIned_dir)
        _in_cp = True
    except ValueError:
        pass
    if not _in_cp:
        _in_cp = ".contAIned" in resolved.parts
    if _in_cp:
        reason = (
            f"Write denied: '{_primary}' is inside the .contAIned/ control-plane directory.\n"
            "Audit logs and policy state are managed by contAIned and must not be edited directly."
        )
        _log(event, _primary, {"outcome": "deny", "reason": reason})
        _deny(reason)
        sys.exit(0)

    # ── Structural pre-check 2: .claude/settings.json ─────────────────────────
    claude_settings = (project_root / ".claude" / "settings.json").resolve()
    _is_claude_settings = (resolved == claude_settings) or (
        ".claude" in resolved.parts and resolved.name == "settings.json"
    )
    if _is_claude_settings:
        reason = (
            f"Write denied: '{_primary}' is the Claude Code settings file.\n"
            "Hook registration is managed by contAIned and must not be edited directly."
        )
        _log(event, _primary, {"outcome": "deny", "reason": reason})
        _deny(reason)
        sys.exit(0)

# ── Engine path ───────────────────────────────────────────────────────────────
try:
    from contained.engine import (
        build_file_path_entity,
        evaluate,
        extract_file_targets,
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

    for target in targets:
        entity   = build_file_path_entity(target, secrets_patterns=patterns)
        decision = evaluate(rules, tool, entity, principal, context=context)

        if decision.outcome == Outcome.DENY:
            _log(event, target, {
                "outcome": "deny",
                "rule_id": decision.rule_id,
                "reason":  decision.reason,
            })
            _deny(decision.reason or f"Write denied: {target}")
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

policy  = load_policy(cwd)
target  = tool_input.get("file_path", "")
if not target:
    sys.exit(0)

resolved_target = Path(target).resolve()
for _action, _pats, _reason in policy["secrets"]["_compiled_rules"]:
    if any(p.search(str(resolved_target)) for p in _pats):
        if _action == "allow":
            break
        _log(event, target, {"outcome": "deny", "reason": _reason})
        _deny(_reason or f"Write denied: '{target}' looks like a secret file.")
        sys.exit(0)

sys.exit(0)
