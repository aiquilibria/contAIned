#!/usr/bin/env python3
"""
PostToolUse hook — records a structured audit entry for every tool execution.

Primary store: tracer.db via contAInedTracer.log_event() (SQLite, concurrent-safe).

Logging is controlled by policy.audit.enabled in manifest.yaml.
This hook must never block execution (always exits 0).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _policy import load_policy  # noqa: E402


try:
    event = json.load(sys.stdin)
except (json.JSONDecodeError, EOFError):
    sys.exit(0)

cwd    = event.get("cwd", ".")
policy = load_policy(cwd)

if not policy["audit"]["enabled"]:
    sys.exit(0)

session_id    = event.get("session_id")
agent_id      = event.get("agent_id")
actor_id      = agent_id or session_id
tool          = event.get("tool_name", "")
tool_input    = event.get("tool_input") or {}
tool_response = event.get("tool_response") or {}

is_error = tool_response.get("is_error", False)
outcome  = "denied" if is_error else "success"

reason = None
if is_error:
    content = tool_response.get("content")
    if isinstance(content, list):
        reason = " ".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        ).strip() or None
    elif isinstance(content, str):
        reason = content or None

# ── Exception detection ───────────────────────────────────────────────────────
# Flag successful WebFetch/WebSearch/Skill/MCP calls that were approved outside
# the policy allowlist — these required operator confirmation and are exceptions.
approved_exception = False
exception_detail: str | None = None
if outcome == "success":
    network_policy = policy.get("network", {})
    allowed_domains = network_policy.get("allowed_domains", [])
    if tool == "WebFetch":
        try:
            from urllib.parse import urlparse  # noqa: PLC0415
            domain = urlparse(tool_input.get("url", "")).hostname or ""
            if domain and domain not in allowed_domains:
                approved_exception = True
                exception_detail = domain
        except Exception:
            pass
    elif tool == "WebSearch":
        approved_exception = True
    elif tool == "Skill":
        skill_name = tool_input.get("skill", "") or tool_input.get("name", "")
        approved_skills = policy.get("skills", {}).get("approved_skills", [])
        if skill_name and skill_name not in approved_skills:
            approved_exception = True
            exception_detail = skill_name
    elif tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        server = parts[1] if len(parts) > 1 else ""
        approved_servers = policy.get("mcp", {}).get("approved_servers", [])
        if server and server not in approved_servers:
            approved_exception = True
            exception_detail = server

# ── Primary store: tracer.db ──────────────────────────────────────────────────
try:
    from contained.tracer import contAInedTracer  # noqa: PLC0415
    db_path = str(Path(cwd) / ".contAIned" / "tracer.db")
    tracer  = contAInedTracer(db_path)
    tracer.log_event(
        session_id        = actor_id,
        tool              = tool,
        tool_input        = tool_input,
        outcome           = outcome,
        reason            = reason,
        tool_response     = tool_response,
        approved_exception = approved_exception,
        exception_detail   = exception_detail,
    )
except Exception:
    pass  # never block execution due to logging failure

sys.exit(0)
