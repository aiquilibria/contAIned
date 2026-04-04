"""Audit logging for the Cedar-inspired engine.

log_decision() serialises a Decision alongside its evaluation context and
appends it to the hook's stdout so the existing audit.py hook can pick it up,
or writes directly to the audit log if called outside a hook context.

All serialisation goes through model_dump() — no ad-hoc dicts — so the
audit record is always consistent with the entity schema.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from contained.engine.entities import AgentSession, Decision


def log_decision(
    decision: Decision,
    action: str,
    resource_entity: Any,
    principal: AgentSession,
    *,
    tool_input: dict | None = None,
    file: Any = None,
) -> None:
    """Write a single JSON audit record for an engine decision.

    The record is written to *file* (defaults to sys.stdout).
    Each call emits one newline-delimited JSON object.

    Fields:
      type       — always "engine_decision"
      outcome    — "allow" | "deny" | "escalate" | "defer"
      rule_id    — rule ID that matched, or null for DEFER
      rule_version — version prefix from rule_id ("v0", "v1", "builtin"), or null
      reason     — human-readable denial/escalation reason, or null
      action     — tool name (e.g. "Bash", "Read")
      resource   — model_dump() of the resource entity, or {"raw": str(entity)}
      principal  — model_dump() of the AgentSession
      tool_input — raw tool_input dict if provided (for hook context)
    """
    if file is None:
        file = sys.stdout

    resource_dict: dict
    if hasattr(resource_entity, "model_dump"):
        resource_dict = resource_entity.model_dump()
    else:
        resource_dict = {"raw": str(resource_entity)}

    record: dict[str, Any] = {
        "type": "engine_decision",
        "outcome": decision.outcome.value,
        "rule_id": decision.rule_id,
        "rule_version": decision.rule_version,
        "reason": decision.reason,
        "action": action,
        "resource": resource_dict,
        "principal": principal.model_dump(),
    }
    if tool_input is not None:
        record["tool_input"] = tool_input

    print(json.dumps(record), file=file)
