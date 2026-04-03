#!/usr/bin/env python3
"""
PreToolUse hook — restricts WebFetch and WebSearch tool calls.

Policy is evaluated by the Cedar-inspired engine (contained.engine).
Falls back to domain allowlist pattern matching if the engine is unavailable.

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


def _log(event: dict, url: str, policy_data: dict) -> None:
    """Append a structured audit entry to pipeline.jsonl. Never raises."""
    try:
        project_root = Path(event.get("cwd", "."))
        audit_log = project_root / ".contAIned" / "audit" / "pipeline.jsonl"
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "session_id": event.get("session_id"),
            "tool":       event.get("tool_name"),
            "input":      {"url": url},
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
            session_id=event.get("agent_id") or event.get("session_id") or "",
            tool=event.get("tool_name", ""),
            tool_input={"url": url},
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

if tool not in ("WebFetch", "WebSearch"):
    sys.exit(0)

url = tool_input.get("url", "") or tool_input.get("query", "")
if not url:
    sys.exit(0)

# ── Engine path ───────────────────────────────────────────────────────────────
try:
    from contained.engine import (
        build_network_resource_entity,
        evaluate,
        load_allowed_domains,
        load_rules,
    )
    from contained.engine.entities import Outcome, build_agent_session, build_context

    principal = build_agent_session(event)
    context   = build_context(event)
    rules     = load_rules()
    domains   = load_allowed_domains()
    entity    = build_network_resource_entity(url, domains)
    decision  = evaluate(rules, tool, entity, principal, context=context)

    if decision.outcome == Outcome.DENY:
        _log(event, url, {
            "outcome": "deny",
            "rule_id": decision.rule_id,
            "reason":  decision.reason,
        })
        _deny(decision.reason or f"Network access denied: {url}")
        sys.exit(0)

    if decision.outcome == Outcome.ALLOW:
        _allow()
        sys.exit(0)

    if decision.outcome == Outcome.ESCALATE:
        _ask()
        sys.exit(0)

    # DEFER — fall through
    sys.exit(0)

# ── Fallback: allowlist check from _policy.py ─────────────────────────────────
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from _policy import load_policy  # noqa: E402

from urllib.parse import urlparse  # noqa: E402

cwd    = event.get("cwd", ".")
policy = load_policy(cwd)

if not policy.get("network", {}).get("enabled", True):
    sys.exit(0)

parsed  = urlparse(url)
domain  = parsed.netloc or parsed.path
allowed = policy.get("network", {}).get("allowed_domains", [])

if domain in allowed:
    sys.exit(0)

_log(event, url, {"outcome": "deny", "reason": f"Domain '{domain}' is not in the network allowlist."})
_deny(f"Network access to '{domain}' is not permitted. Domain is not in the allowlist.")
sys.exit(0)
