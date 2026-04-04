"""Core evaluation algorithm for the Cedar-inspired policy engine.

evaluate() is a pure function — no I/O, no side effects. It reads entities
and returns a Decision. Logging happens in the hook, not here.
"""

from __future__ import annotations

from typing import Any

from contained.engine.conditions import evaluate_condition
from contained.engine.entities import AgentSession, BashCommand, Decision, Outcome, Rule

_COMPOUND_BASH_REASON = (
    "Compound shell commands are not permitted. "
    "Single-verb rules cannot be sound across compound commands. "
    "Express the specific allowed form as a manifest rule if needed."
)

_FAIL_CLOSED_REASON = "[policy evaluation error — failing closed]"


def evaluate(
    rules: list[Rule],
    action: str,
    resource_entity: Any,
    principal: AgentSession,
    context: dict[str, Any],
) -> Decision:
    """Cedar-inspired evaluation algorithm.

    Structural pre-checks (before any rule evaluation):
      - Bash compound commands (is_compound == True) → unconditional DENY.
        Defense-in-depth only. The primary control against shell delegation is
        the block-shell-delegation manifest rule.

    Evaluation order:
      1. Collect all rules whose scope (action + resource_type) matches.
      2. For each matching rule, evaluate when/unless conditions.
         - forbid rules: AttributeError → treat rule as matching (fail-closed).
         - permit/escalate rules: AttributeError → skip rule (conservative).
      3. If any forbid rule is satisfied → DENY (first satisfied forbid wins).
      4. Else if any escalate rule is satisfied → ESCALATE (first wins).
      5. Else if any permit rule is satisfied → ALLOW (first wins).
      6. Else → DEFER.

    when conditions: combined with AND (all must hold).
    unless conditions: combined with OR (any one being true negates the rule).
    """
    # --- Structural pre-check: compound bash ---
    if (
        action == "Bash"
        and isinstance(resource_entity, BashCommand)
        and resource_entity.is_compound
    ):
        return Decision(
            outcome=Outcome.DENY,
            rule_id="builtin:compound-bash",
            reason=_COMPOUND_BASH_REASON,
        )

    satisfied_forbids: list[tuple[Rule, str | None]] = []
    satisfied_escalates: list[Rule] = []
    satisfied_permits: list[Rule] = []

    for rule in rules:
        if rule.effect == "define":
            continue  # classifier rules are not enforcement rules
        if not _action_matches(rule, action):
            continue
        if not _resource_type_matches(rule, resource_entity):
            continue

        try:
            when_result = all(
                evaluate_condition(c, resource_entity, principal, context) for c in rule.when
            )
            unless_result = any(
                evaluate_condition(c, resource_entity, principal, context) for c in rule.unless
            )
        except AttributeError:
            if rule.effect == "forbid":
                # Fail-closed: a forbid rule whose conditions cannot be evaluated
                # is treated as matching. Reason includes diagnostic context.
                satisfied_forbids.append((rule, _FAIL_CLOSED_REASON))
            # permit/escalate: skip on error (uncertain permission is withheld).
            continue
        except Exception:
            # Any other evaluation error: same policy as AttributeError.
            if rule.effect == "forbid":
                satisfied_forbids.append((rule, _FAIL_CLOSED_REASON))
            continue

        if when_result and not unless_result:
            if rule.effect == "forbid":
                satisfied_forbids.append((rule, rule.reason))
            elif rule.effect == "escalate":
                satisfied_escalates.append(rule)
            elif rule.effect == "permit":
                satisfied_permits.append(rule)

    if satisfied_forbids:
        rule, reason = satisfied_forbids[0]
        return Decision(outcome=Outcome.DENY, rule_id=rule.id, reason=reason)

    if satisfied_escalates:
        r = satisfied_escalates[0]
        return Decision(outcome=Outcome.ESCALATE, rule_id=r.id, reason=r.reason)

    if satisfied_permits:
        r = satisfied_permits[0]
        return Decision(outcome=Outcome.ALLOW, rule_id=r.id, reason=None)

    return Decision(outcome=Outcome.DEFER)


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


def _action_matches(rule: Rule, action: str) -> bool:
    return "*" in rule.action or action in rule.action


def _resource_type_matches(rule: Rule, entity: Any) -> bool:
    if rule.resource_type == "*":
        return True
    return type(entity).__name__ == rule.resource_type
