"""Condition parser and evaluator for the Cedar-inspired policy engine.

Conditions are attribute comparison strings evaluated against entity objects.
The supported operator set is deliberately constrained — no arbitrary Python
expression evaluation.

Supported operators:
  ==           equality (bool, str, int, None)
  !=           inequality
  in           set membership: resource.verb in ["git", "go"]
  not in       set non-membership
  contains     list containment: resource.args contains "--force"
               (reverse of `in` — literal is in a list-valued attribute)
  matches      fnmatch glob against string attribute
  not matches  negated glob
  matches_re   regex match (Phase 1 compat only — for translating old bash rules)
  >  >=  <  <= numeric comparison
  is null      null / None check
  is not null  non-null check

Attribute references: resource.X, principal.X, context.X
AttributeError propagates up for unknown attribute names so engine.py can
apply fail-closed logic for forbid rules.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_condition(
    condition: str,
    resource: Any,
    principal: Any,
    context: dict[str, Any],
) -> bool:
    """Parse and evaluate a single condition string.

    Raises AttributeError if the condition references an unknown attribute,
    so the engine can apply fail-closed logic for forbid rules.
    Raises ValueError for malformed condition syntax.
    """
    condition = condition.strip()

    # --- is null / is not null (must check before other operators) ---
    if condition.endswith(" is not null"):
        attr = condition[: -len(" is not null")].strip()
        return _resolve(attr, resource, principal, context) is not None
    if condition.endswith(" is null"):
        attr = condition[: -len(" is null")].strip()
        return _resolve(attr, resource, principal, context) is None

    # --- two-token operators that need careful splitting ---
    for op in (
        "not matches",
        "not in",
        "matches_re",
        "contains",
        "matches",
        ">=",
        "<=",
        "!=",
        "==",
        ">",
        "<",
        "in",
    ):
        lhs, found, rhs = condition.partition(f" {op} ")
        if found:
            return _apply(op, lhs.strip(), rhs.strip(), resource, principal, context)

    raise ValueError(f"Unrecognised condition syntax: {condition!r}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve(ref: str, resource: Any, principal: Any, context: dict[str, Any]) -> Any:
    """Resolve a dot-notation attribute reference against the entity objects.

    Raises AttributeError if the attribute does not exist on the entity,
    so the engine can handle fail-closed semantics for forbid rules.
    """
    parts = ref.split(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid attribute reference {ref!r} (expected dot notation)")
    namespace, attr = parts

    if namespace == "resource":
        return getattr(resource, attr)  # AttributeError propagates on unknown attr
    if namespace == "principal":
        return getattr(principal, attr)
    if namespace == "context":
        if attr not in context:
            raise AttributeError(f"context.{attr} is not available in this phase")
        return context[attr]

    raise ValueError(f"Unknown namespace {namespace!r} in {ref!r}")


def _parse_rhs(raw: str) -> Any:
    """Parse a literal RHS value from a condition string.

    Handles: quoted strings, booleans, integers, null, and YAML-style lists.
    """
    raw = raw.strip()

    # List literal: ["a", "b", ...]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        items = []
        for item in _split_list(inner):
            items.append(_parse_scalar(item.strip()))
        return items

    return _parse_scalar(raw)


def _parse_scalar(raw: str) -> Any:
    """Parse a single scalar literal."""
    raw = raw.strip()
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw  # bare unquoted string


def _split_list(inner: str) -> list[str]:
    """Split a comma-separated list interior, respecting quoted strings."""
    items: list[str] = []
    depth = 0
    current: list[str] = []
    in_quote: str | None = None

    for ch in inner:
        if in_quote:
            current.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in ('"', "'"):
            in_quote = ch
            current.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)

    if current:
        items.append("".join(current))
    return items


def _apply(
    op: str,
    lhs_ref: str,
    rhs_raw: str,
    resource: Any,
    principal: Any,
    context: dict[str, Any],
) -> bool:
    """Apply an operator between a LHS reference and a RHS literal."""

    # `contains` is special: LHS is an attribute (list), RHS is a literal scalar.
    # e.g. resource.args contains "--force"
    if op == "contains":
        lhs_val = _resolve(lhs_ref, resource, principal, context)
        rhs_val = _parse_rhs(rhs_raw)
        if not isinstance(lhs_val, list):
            raise TypeError(
                f"'contains' operator requires a list attribute; "
                f"{lhs_ref!r} is {type(lhs_val).__name__}"
            )
        return rhs_val in lhs_val

    # `in` can run in two directions:
    #   (a) resource.verb in ["git", "go"]    — attr value in literal list
    #   (b) "--force" in resource.args         — literal in list-valued attr
    # Detect direction by checking whether lhs_ref starts with a namespace prefix.
    if op == "in":
        if lhs_ref.startswith(("resource.", "principal.", "context.")):
            # Direction (a): attr value in list
            lhs_val = _resolve(lhs_ref, resource, principal, context)
            rhs_val = _parse_rhs(rhs_raw)
            if not isinstance(rhs_val, list):
                raise TypeError(f"'in' RHS must be a list; got {rhs_raw!r}")
            return lhs_val in rhs_val
        else:
            # Direction (b): literal in list-valued attr
            lhs_val = _parse_rhs(lhs_ref)
            rhs_val = _resolve(rhs_raw, resource, principal, context)
            if not isinstance(rhs_val, list):
                raise TypeError(
                    f"'in' with literal LHS requires a list-valued attribute; "
                    f"{rhs_raw!r} is {type(rhs_val).__name__}"
                )
            return lhs_val in rhs_val

    if op == "not in":
        if lhs_ref.startswith(("resource.", "principal.", "context.")):
            lhs_val = _resolve(lhs_ref, resource, principal, context)
            rhs_val = _parse_rhs(rhs_raw)
            if not isinstance(rhs_val, list):
                raise TypeError(f"'not in' RHS must be a list; got {rhs_raw!r}")
            return lhs_val not in rhs_val
        else:
            lhs_val = _parse_rhs(lhs_ref)
            rhs_val = _resolve(rhs_raw, resource, principal, context)
            if not isinstance(rhs_val, list):
                raise TypeError("'not in' with literal LHS requires a list-valued attribute")
            return lhs_val not in rhs_val

    # All remaining operators: LHS is always an attribute reference.
    lhs_val = _resolve(lhs_ref, resource, principal, context)
    rhs_val = _parse_rhs(rhs_raw)

    if op == "==":
        return lhs_val == rhs_val
    if op == "!=":
        return lhs_val != rhs_val
    if op == "matches":
        if not isinstance(lhs_val, str):
            raise TypeError(f"'matches' requires a string attribute; got {type(lhs_val).__name__}")
        return fnmatch.fnmatch(lhs_val, str(rhs_val))
    if op == "not matches":
        if not isinstance(lhs_val, str):
            raise TypeError(
                f"'not matches' requires a string attribute; got {type(lhs_val).__name__}"
            )
        return not fnmatch.fnmatch(lhs_val, str(rhs_val))
    if op == "matches_re":
        # Phase 1 compat only: regex match against a string attribute.
        if not isinstance(lhs_val, str):
            raise TypeError(
                f"'matches_re' requires a string attribute; got {type(lhs_val).__name__}"
            )
        return bool(re.search(str(rhs_val), lhs_val))
    # TODO: extend numeric comparisons to support semver strings
    #   e.g. resource.go_version >= "1.23.0" using packaging.version.Version
    #   or a lightweight semver parser. Currently coerces both sides to float,
    #   which works for tool_call_count and similar integer attributes but
    #   silently fails for version strings like "1.23.0".
    if op == ">":
        return float(lhs_val) > float(rhs_val)
    if op == ">=":
        return float(lhs_val) >= float(rhs_val)
    if op == "<":
        return float(lhs_val) < float(rhs_val)
    if op == "<=":
        return float(lhs_val) <= float(rhs_val)

    raise ValueError(f"Unknown operator {op!r}")
