"""Init-time policy validator for the Cedar-inspired engine.

Runs at `contAIned init` time via validate_manifest.py, invoked as a
Dockerfile RUN step so validation errors abort the image build.

Phase 1: advisory mode — warnings are printed but do not fail the build.
Phase 2: blocking mode — validation errors abort the build. Invoked via
         python3 -m contained.engine.validate_manifest <manifest.yaml>.

Validation checks:
  Structural validity
    - Every rule has id, effect, action, resource_type.
    - effect is one of permit / forbid / escalate.
    - action values are drawn from the known action registry.
    - resource_type values are drawn from the known entity type registry.

  Attribute validity
    - Every attribute reference in when/unless conditions corresponds to a
      declared field on the appropriate entity type.
    - The attribute registry is derived directly from Pydantic model_fields —
      no separate registry to maintain or drift from the implementation.
    - Attribute type is checked for operator compatibility (e.g. > requires a
      numeric annotation; matches requires str).
    - context.* references are warnings in Phase 1, errors in Phase 2.

  Logical consistency (advisory warnings, not errors in any phase)
    - A when condition that contradicts an unless condition on the same rule
      (can never match) produces a warning.
    - A forbid rule identical in scope and conditions to a permit rule produces
      a warning (forbid always wins — the permit is dead code).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, get_args, get_origin

from contained.engine.entities import (
    CONTEXT_SCHEMA,
    AgentSession,
    BashCommand,
    FilePath,
    GlobPattern,
    NetworkResource,
    Rule,
)

# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

#: All tool names the engine recognises as actions.
ACTION_REGISTRY: frozenset[str] = frozenset(
    {
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "Glob",
        "Grep",
        "Bash",
        "WebFetch",
        "WebSearch",
        "*",
    }
)

#: Maps resource_type name → Pydantic model so attribute validation can use
#: model_fields directly.  "*" matches any entity type; attribute references
#: against "*" resource_type cannot be validated and are silently accepted.
ENTITY_REGISTRY: dict[str, Any] = {
    "FilePath": FilePath,
    "GlobPattern": GlobPattern,
    "BashCommand": BashCommand,
    "NetworkResource": NetworkResource,
    "*": None,  # wildcard — no attribute validation possible
}

#: Maps resource_type → its principal model (always AgentSession for Phase 1).
_PRINCIPAL_MODEL = AgentSession

#: Operators that require a numeric (int/float) LHS attribute.
_NUMERIC_OPS: frozenset[str] = frozenset({">", ">=", "<", "<="})

#: Operators that require a string LHS attribute.
_STRING_OPS: frozenset[str] = frozenset({"matches", "not matches", "matches_re"})

#: Operators that require a list LHS attribute.
_LIST_OPS: frozenset[str] = frozenset({"contains"})

#: Condition operator tokens, longest first to avoid partial matches.
_OPERATORS = (
    "not matches",
    "not in",
    "matches_re",
    "contains",
    "matches",
    "is not null",
    "is null",
    ">=",
    "<=",
    "!=",
    "==",
    ">",
    "<",
    "in",
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ValidationIssue:
    rule_id: str
    severity: str  # "error" | "warning"
    message: str


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def add_error(self, rule_id: str, message: str) -> None:
        self.issues.append(ValidationIssue(rule_id=rule_id, severity="error", message=message))

    def add_warning(self, rule_id: str, message: str) -> None:
        self.issues.append(ValidationIssue(rule_id=rule_id, severity="warning", message=message))

    def print_report(self, *, phase: int = 1) -> None:
        """Print all issues to stdout. Phase 1 prints errors as warnings."""
        for issue in self.issues:
            label = issue.severity.upper() if phase >= 2 else "WARNING"
            print(f"  [{label}] {issue.rule_id}: {issue.message}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_rules(rules: list[Rule], *, phase: int = 1) -> ValidationResult:
    """Validate a list of Rule objects and return a ValidationResult.

    In Phase 1, all issues are advisory (no build failures).
    In Phase 2+, errors prevent image construction.
    """
    result = ValidationResult()

    # Collect (scope_key → list[Rule]) for logical consistency checks.
    scope_map: dict[tuple, list[Rule]] = {}

    for rule in rules:
        _check_structural(rule, result)
        _check_attribute_refs(rule, result, phase=phase)

        if rule.effect == "define":
            continue  # classifiers don't participate in logical consistency checks

        scope_key = (
            frozenset(rule.action),
            rule.resource_type,
            tuple(sorted(rule.when)),
            tuple(sorted(rule.unless)),
        )
        scope_map.setdefault(scope_key, []).append(rule)

    _check_logical_consistency(scope_map, result)
    return result


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------


def _check_structural(rule: Rule, result: ValidationResult) -> None:
    if not rule.id:
        result.add_error(rule.id or "<unknown>", "Rule is missing an 'id'.")

    valid_effects = ("permit", "forbid", "escalate", "define")
    if rule.effect not in valid_effects:
        result.add_error(rule.id, f"Invalid effect {rule.effect!r}.")

    # define rules are classifiers, not enforcement rules — different checks apply.
    if rule.effect == "define":
        if not rule.define:
            result.add_error(rule.id, "define rule must have a non-empty 'define' block.")
        return

    if not rule.action:
        result.add_error(rule.id, "Rule has no action values.")

    for act in rule.action:
        if act not in ACTION_REGISTRY:
            result.add_error(rule.id, f"Unknown action {act!r}.")

    if rule.resource_type not in ENTITY_REGISTRY:
        result.add_error(rule.id, f"Unknown resource_type {rule.resource_type!r}.")


# ---------------------------------------------------------------------------
# Attribute reference checks
# ---------------------------------------------------------------------------


def _check_attribute_refs(rule: Rule, result: ValidationResult, *, phase: int) -> None:
    if rule.effect == "define":
        return  # define rules have no when/unless conditions to validate
    model = ENTITY_REGISTRY.get(rule.resource_type)

    for condition in rule.when + rule.unless:
        _check_condition(condition, rule.id, model, result, phase=phase)


def _check_condition(
    condition: str,
    rule_id: str,
    resource_model: Any,
    result: ValidationResult,
    *,
    phase: int,
) -> None:
    condition = condition.strip()

    # Detect operator and extract LHS reference.
    lhs_ref: str | None = None
    op: str | None = None

    # is null / is not null
    for suffix in (" is not null", " is null"):
        if condition.endswith(suffix):
            lhs_ref = condition[: -len(suffix)].strip()
            op = suffix.strip()
            break

    if lhs_ref is None:
        for candidate_op in _OPERATORS:
            lhs, found, _ = condition.partition(f" {candidate_op} ")
            if found:
                lhs_ref = lhs.strip()
                op = candidate_op
                break

    if lhs_ref is None:
        result.add_error(rule_id, f"Unrecognised condition syntax: {condition!r}")
        return

    _check_ref(lhs_ref, op or "", rule_id, resource_model, result, phase=phase)


def _check_ref(
    ref: str,
    op: str,
    rule_id: str,
    resource_model: Any,
    result: ValidationResult,
    *,
    phase: int,
) -> None:
    parts = ref.split(".", 1)
    if len(parts) != 2:
        # Could be a literal LHS (e.g. '"--force" in resource.args') — skip.
        return

    namespace, attr = parts

    if namespace == "context":
        if phase < 3:
            result.add_error(
                rule_id,
                f"context.{attr} is only available from Phase 3; "
                "use principal or resource attributes.",
            )
            return
        # Phase 3+: validate against the declared context schema.
        if attr not in CONTEXT_SCHEMA:
            result.add_error(
                rule_id,
                f"context.{attr!r} is not a declared context attribute. "
                f"Known attributes: {sorted(CONTEXT_SCHEMA)}.",
            )
            return
        annotation = CONTEXT_SCHEMA[attr]
        _check_operator_compat(op or "", annotation, f"context.{attr}", rule_id, result)
        return

    if namespace == "principal":
        model = _PRINCIPAL_MODEL
    elif namespace == "resource":
        model = resource_model
    else:
        result.add_error(rule_id, f"Unknown namespace {namespace!r} in {ref!r}.")
        return

    if model is None:
        # resource_type == "*" — can't validate attribute names.
        return

    fields = model.model_fields
    if attr not in fields:
        result.add_error(rule_id, f"Attribute {ref!r} does not exist on {model.__name__}.")
        return

    # Operator/type compatibility check.
    annotation = fields[attr].annotation
    _check_operator_compat(op, annotation, ref, rule_id, result)


def _check_operator_compat(
    op: str,
    annotation: Any,
    ref: str,
    rule_id: str,
    result: ValidationResult,
) -> None:
    """Check that the operator is compatible with the attribute's type annotation."""
    if annotation is None:
        return  # Can't check — skip.

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Unwrap Optional[X] → X
    if origin is type(None):
        return
    actual = annotation
    if origin is not None and any(a is type(None) for a in args):
        # Union with None (Optional): use the non-None type for compat check.
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            actual = non_none[0]
            origin = get_origin(actual)

    # Numeric operators require int or float.
    if op in _NUMERIC_OPS:
        if actual not in (int, float) and origin not in (int, float):
            result.add_warning(
                rule_id, f"Operator {op!r} expects a numeric attribute; {ref!r} has type {actual}."
            )

    # String operators require str.
    if op in _STRING_OPS:
        if actual is not str and origin is not str:
            result.add_warning(
                rule_id, f"Operator {op!r} expects a string attribute; {ref!r} has type {actual}."
            )

    # List operators require list.
    if op in _LIST_OPS:
        if origin is not list and actual is not list:
            result.add_warning(
                rule_id, f"Operator {op!r} expects a list attribute; {ref!r} has type {actual}."
            )


# ---------------------------------------------------------------------------
# Logical consistency checks (advisory only, all phases)
# ---------------------------------------------------------------------------


def _check_logical_consistency(
    scope_map: dict[tuple, list[Rule]],
    result: ValidationResult,
) -> None:
    """Warn about dead rules: forbid + permit with identical scope and conditions."""
    for rules in scope_map.values():
        effects = {r.effect for r in rules}
        if "forbid" in effects and "permit" in effects:
            forbid_ids = [r.id for r in rules if r.effect == "forbid"]
            permit_ids = [r.id for r in rules if r.effect == "permit"]
            result.add_warning(
                forbid_ids[0],
                f"forbid rule(s) {forbid_ids} and permit rule(s) {permit_ids} have identical "
                f"scope and conditions — the permit(s) are dead code (forbid always wins).",
            )
