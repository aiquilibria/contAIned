"""Tests for the evaluate() core algorithm."""

from __future__ import annotations

import re

from contained.engine.engine import evaluate
from contained.engine.entities import (
    AgentSession,
    Decision,
    Outcome,
    Rule,
    build_file_path_entity,
)
from tests.engine.conftest import (
    fixture_bash,
    fixture_file_path,
    fixture_network,
    load_fixture_rules,
)

# ---------------------------------------------------------------------------
# Helpers for context-conditional tests
# ---------------------------------------------------------------------------


def _ctx(**kwargs) -> dict:
    """Build a minimal context dict with safe defaults, applying overrides."""
    base: dict = {"task_phase": "active", "qa_status": "unknown", "tool_call_count": 0}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(**kwargs) -> AgentSession:
    return AgentSession(session_id="test", **kwargs)


def _ev(rules, action, resource, principal=None, context=None) -> Decision:
    return evaluate(rules, action, resource, principal or _session(), context or {})


_DOTENV_PATTERNS = [
    ("allow", [re.compile(r"\.(example|sample|template)", re.IGNORECASE)], ""),
    (
        "block",
        [re.compile(r"(^|[/\\])\.env(\.[^/\\]+)?$", re.IGNORECASE)],
        "Secret files may not be accessed.",
    ),
]

RULES = load_fixture_rules()


# ---------------------------------------------------------------------------
# DEFER: no rules match
# ---------------------------------------------------------------------------


class TestDefer:
    def test_no_rules_returns_defer(self):
        fp = fixture_file_path("/workspace/main.py")
        d = _ev([], "Read", fp)
        assert d.outcome == Outcome.DEFER
        assert d.rule_id is None

    def test_no_matching_action_defers(self):
        rule = Rule(
            id="v1:test:read-only",
            effect="forbid",
            action=["Read"],
            resource_type="FilePath",
            when=["resource.is_secret == true"],
        )
        fp = fixture_file_path("/workspace/.env", _DOTENV_PATTERNS)
        # Write action — rule only covers Read → DEFER
        d = _ev([rule], "Write", fp)
        assert d.outcome == Outcome.DEFER

    def test_no_matching_resource_type_defers(self):
        rule = Rule(
            id="v1:test:file-only",
            effect="forbid",
            action=["Bash"],
            resource_type="BashCommand",
            when=['resource.verb == "rm"'],
        )
        fp = fixture_file_path("/workspace/main.py")
        d = _ev([rule], "Bash", fp)  # FilePath entity, not BashCommand → DEFER
        assert d.outcome == Outcome.DEFER


# ---------------------------------------------------------------------------
# DENY: forbid rule
# ---------------------------------------------------------------------------


class TestDeny:
    def test_forbid_secret_file(self):
        rules = [
            Rule(
                id="v1:secrets:block",
                effect="forbid",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.is_secret == true"],
                reason="No secrets.",
            )
        ]
        fp = fixture_file_path("/workspace/.env", _DOTENV_PATTERNS)
        d = _ev(rules, "Read", fp)
        assert d.outcome == Outcome.DENY
        assert d.rule_id == "v1:secrets:block"
        assert d.reason == "No secrets."

    def test_forbid_wins_over_permit(self):
        rules = [
            Rule(
                id="v1:test:forbid",
                effect="forbid",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.is_workspace == false"],
                reason="Not in workspace.",
            ),
            Rule(
                id="v1:test:permit",
                effect="permit",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.in_workspace == false"],
            ),
        ]
        # Use a FilePath that's outside the workspace.
        fp = build_file_path_entity("/etc/passwd", secrets_patterns=[])
        # The forbid rule checks resource.is_workspace
        # (nonexistent attr → AttributeError → fail-closed)
        d = _ev(rules, "Read", fp)
        assert d.outcome == Outcome.DENY

    def test_first_forbid_wins(self):
        """When multiple forbid rules match, the first one's rule_id is used."""
        rules = [
            Rule(
                id="v1:first",
                effect="forbid",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.is_secret == true"],
                reason="First.",
            ),
            Rule(
                id="v1:second",
                effect="forbid",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.is_secret == true"],
                reason="Second.",
            ),
        ]
        fp = fixture_file_path("/workspace/.env", _DOTENV_PATTERNS)
        d = _ev(rules, "Read", fp)
        assert d.rule_id == "v1:first"

    def test_bash_compound_builtin_deny(self):
        """Compound bash commands are denied by the structural pre-check."""
        bc = fixture_bash("git status && git diff")
        d = _ev([], "Bash", bc)
        assert d.outcome == Outcome.DENY
        assert d.rule_id == "builtin:compound-bash"

    def test_bash_compound_precheck_before_rules(self):
        """Structural pre-check fires even when no rules are loaded."""
        bc = fixture_bash("ls; rm -rf /")
        d = _ev([], "Bash", bc)
        assert d.outcome == Outcome.DENY


# ---------------------------------------------------------------------------
# ALLOW: permit rule
# ---------------------------------------------------------------------------


class TestAllow:
    def test_permit_rule_allows(self):
        rules = [
            Rule(
                id="v1:test:permit-workspace",
                effect="permit",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.in_workspace == true"],
            )
        ]
        fp = fixture_file_path("/workspace/main.py")
        d = _ev(rules, "Read", fp)
        assert d.outcome == Outcome.ALLOW
        assert d.rule_id == "v1:test:permit-workspace"

    def test_first_permit_wins(self):
        rules = [
            Rule(
                id="v1:first",
                effect="permit",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.in_workspace == true"],
            ),
            Rule(
                id="v1:second",
                effect="permit",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.in_workspace == true"],
            ),
        ]
        fp = fixture_file_path("/workspace/main.py")
        d = _ev(rules, "Read", fp)
        assert d.rule_id == "v1:first"


# ---------------------------------------------------------------------------
# ESCALATE
# ---------------------------------------------------------------------------


class TestEscalate:
    def test_escalate_rule(self):
        rules = [
            Rule(
                id="v1:test:escalate",
                effect="escalate",
                action=["Bash"],
                resource_type="BashCommand",
                when=['resource.verb == "git"', 'resource.subcommand == "push"'],
                reason="git push requires operator approval.",
            )
        ]
        bc = fixture_bash("git push origin main")
        d = _ev(rules, "Bash", bc)
        assert d.outcome == Outcome.ESCALATE
        assert d.rule_id == "v1:test:escalate"
        assert d.reason == "git push requires operator approval."

    def test_forbid_overrides_escalate(self):
        rules = [
            Rule(
                id="v1:forbid",
                effect="forbid",
                action=["Bash"],
                resource_type="BashCommand",
                when=['resource.verb == "git"'],
                reason="No git.",
            ),
            Rule(
                id="v1:escalate",
                effect="escalate",
                action=["Bash"],
                resource_type="BashCommand",
                when=['resource.verb == "git"'],
            ),
        ]
        bc = fixture_bash("git push")
        d = _ev(rules, "Bash", bc)
        assert d.outcome == Outcome.DENY


# ---------------------------------------------------------------------------
# unless semantics (OR — any one true negates the rule)
# ---------------------------------------------------------------------------


class TestUnless:
    def test_unless_prevents_forbid(self):
        """Safe variant satisfies the unless clause, preventing the forbid."""
        rules = [
            Rule(
                id="v1:secrets:block",
                effect="forbid",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.is_secret == true"],
                unless=["resource.is_safe_variant == true"],
                reason="No secrets.",
            )
        ]
        fp = fixture_file_path("/workspace/.env.example", _DOTENV_PATTERNS)
        assert fp.is_secret is True
        assert fp.is_safe_variant is True
        d = _ev(rules, "Read", fp)
        assert d.outcome == Outcome.DEFER

    def test_unless_or_semantics_any_true_negates(self):
        """If any unless condition is true, the rule is not satisfied."""
        rules = [
            Rule(
                id="v1:test:rule",
                effect="forbid",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.in_workspace == true"],
                unless=[
                    "resource.is_safe_variant == true",
                    "resource.in_control_plane == true",
                ],
                reason="Blocked.",
            )
        ]
        fp = fixture_file_path("/workspace/.contAIned/hooks/qa.py")
        # in_workspace=True (when satisfied), in_control_plane=True (unless satisfied)
        d = _ev(rules, "Read", fp)
        assert d.outcome == Outcome.DEFER


# ---------------------------------------------------------------------------
# Wildcard action
# ---------------------------------------------------------------------------


class TestWildcardAction:
    def test_wildcard_action_matches_any(self):
        rules = [
            Rule(
                id="v1:test:wildcard",
                effect="forbid",
                action=["*"],
                resource_type="FilePath",
                when=["resource.in_control_plane == true"],
                reason="Control plane protected.",
            )
        ]
        fp = fixture_file_path("/workspace/.contAIned/hooks/qa.py")
        for action in ("Read", "Write", "Edit", "Grep"):
            d = _ev(rules, action, fp)
            assert d.outcome == Outcome.DENY, f"{action} should be denied"

    def test_wildcard_resource_type(self):
        rules = [
            Rule(
                id="v1:test:wildcard-resource",
                effect="forbid",
                action=["*"],
                resource_type="*",
                when=["resource.in_control_plane == true"],
                reason="Blocked.",
            )
        ]
        fp = fixture_file_path("/workspace/.contAIned/hooks/qa.py")
        d = _ev(rules, "Read", fp)
        assert d.outcome == Outcome.DENY


# ---------------------------------------------------------------------------
# Fail-closed: AttributeError on forbid rules
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_unknown_attr_on_forbid_fails_closed(self):
        """A forbid rule whose condition references an unknown attribute → DENY."""
        rules = [
            Rule(
                id="v1:test:forbid-unknown-attr",
                effect="forbid",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.nonexistent_field == true"],
                reason="Should fail-closed.",
            )
        ]
        fp = fixture_file_path("/workspace/main.py")
        d = _ev(rules, "Read", fp)
        assert d.outcome == Outcome.DENY
        assert "policy evaluation error" in (d.reason or "")

    def test_unknown_attr_on_permit_skips(self):
        """A permit rule whose condition references an unknown attribute → skip → DEFER."""
        rules = [
            Rule(
                id="v1:test:permit-unknown-attr",
                effect="permit",
                action=["Read"],
                resource_type="FilePath",
                when=["resource.nonexistent_field == true"],
            )
        ]
        fp = fixture_file_path("/workspace/main.py")
        d = _ev(rules, "Read", fp)
        assert d.outcome == Outcome.DEFER


# ---------------------------------------------------------------------------
# Fixture rules integration (rules_default.yaml)
# ---------------------------------------------------------------------------


class TestFixtureRules:
    def test_secret_file_denied(self):
        fp = fixture_file_path("/workspace/.env", _DOTENV_PATTERNS)
        d = _ev(RULES, "Read", fp)
        assert d.outcome == Outcome.DENY

    def test_safe_variant_allowed(self):
        fp = fixture_file_path("/workspace/.env.example", _DOTENV_PATTERNS)
        d = _ev(RULES, "Read", fp)
        assert d.outcome == Outcome.DEFER  # unless clause prevents forbid → DEFER

    def test_normal_file_defers(self):
        fp = fixture_file_path("/workspace/src/main.py")
        d = _ev(RULES, "Read", fp)
        assert d.outcome == Outcome.DEFER

    def test_control_plane_write_denied(self):
        fp = fixture_file_path("/workspace/.contAIned/hooks/qa.py")
        d = _ev(RULES, "Write", fp)
        assert d.outcome == Outcome.DENY

    def test_shell_delegation_denied(self):
        bc = fixture_bash("bash -c 'rm -rf /'")
        d = _ev(RULES, "Bash", bc)
        assert d.outcome == Outcome.DENY

    def test_rm_denied(self):
        bc = fixture_bash("rm foo.txt")
        d = _ev(RULES, "Bash", bc)
        assert d.outcome == Outcome.DENY

    def test_network_out_of_allowlist_denied(self):
        nr = fixture_network("https://evil.example.com", allowed_domains=["api.anthropic.com"])
        d = _ev(RULES, "WebFetch", nr)
        assert d.outcome == Outcome.DENY

    def test_network_in_allowlist_defers(self):
        nr = fixture_network(
            "https://api.anthropic.com/v1/messages", allowed_domains=["api.anthropic.com"]
        )
        d = _ev(RULES, "WebFetch", nr)
        assert d.outcome == Outcome.DEFER


# ---------------------------------------------------------------------------
# Context-conditional evaluation (Phase 3)
# ---------------------------------------------------------------------------


class TestContextConditions:
    """Verify that context.* attributes are evaluated correctly in conditions."""

    def _rule(
        self,
        when: list[str],
        effect: str = "forbid",
        **kwargs,
    ) -> Rule:
        return Rule(
            id="v3:test:ctx",
            effect=effect,  # type: ignore[arg-type]
            action=["Bash"],
            resource_type="BashCommand",
            when=when,
            **kwargs,
        )

    def test_forbid_fires_when_context_task_phase_matches(self):
        """context.task_phase == "review" → DENY when context has task_phase=review."""
        rule = self._rule(
            when=['context.task_phase == "review"'], reason="No pushes outside review."
        )
        bc = fixture_bash("git push origin main")
        ctx = _ctx(task_phase="review")
        d = evaluate([rule], "Bash", bc, _session(), ctx)
        assert d.outcome == Outcome.DENY
        assert d.rule_id == "v3:test:ctx"

    def test_forbid_does_not_fire_when_context_does_not_match(self):
        """context.task_phase == "review" → DEFER when task_phase=active."""
        rule = self._rule(when=['context.task_phase == "review"'])
        bc = fixture_bash("git push origin main")
        ctx = _ctx(task_phase="active")
        d = evaluate([rule], "Bash", bc, _session(), ctx)
        assert d.outcome == Outcome.DEFER

    def test_permit_fires_when_qa_status_passing(self):
        """context.qa_status == "passing" → ALLOW (permit overrides defer)."""
        rule = Rule(
            id="v3:test:ctx-permit",
            effect="permit",
            action=["Bash"],
            resource_type="BashCommand",
            when=['context.qa_status == "passing"'],
        )
        bc = fixture_bash("git push origin main")
        ctx = _ctx(qa_status="passing")
        d = evaluate([rule], "Bash", bc, _session(), ctx)
        assert d.outcome == Outcome.ALLOW

    def test_defer_when_context_count_below_threshold(self):
        """context.tool_call_count > 100 → DEFER when count=5."""
        rule = self._rule(when=["context.tool_call_count > 100"])
        bc = fixture_bash("git push origin main")
        ctx = _ctx(tool_call_count=5)
        d = evaluate([rule], "Bash", bc, _session(), ctx)
        assert d.outcome == Outcome.DEFER

    def test_forbid_fires_when_count_exceeds_threshold(self):
        """context.tool_call_count > 100 → DENY when count=150."""
        rule = self._rule(when=["context.tool_call_count > 100"], reason="Too many calls.")
        bc = fixture_bash("git push origin main")
        ctx = _ctx(tool_call_count=150)
        d = evaluate([rule], "Bash", bc, _session(), ctx)
        assert d.outcome == Outcome.DENY

    def test_missing_context_attr_on_forbid_fails_closed(self):
        """context.nonexistent → AttributeError → DENY (fail-closed for forbid)."""
        rule = self._rule(when=["context.nonexistent == true"])
        bc = fixture_bash("git push origin main")
        ctx = _ctx()  # no 'nonexistent' key
        d = evaluate([rule], "Bash", bc, _session(), ctx)
        assert d.outcome == Outcome.DENY
        assert "policy evaluation error" in (d.reason or "")

    def test_missing_context_attr_on_permit_skips(self):
        """context.nonexistent on a permit rule → skipped → DEFER."""
        rule = Rule(
            id="v3:test:ctx-permit-bad",
            effect="permit",
            action=["Bash"],
            resource_type="BashCommand",
            when=["context.nonexistent == true"],
        )
        bc = fixture_bash("git push origin main")
        ctx = _ctx()
        d = evaluate([rule], "Bash", bc, _session(), ctx)
        assert d.outcome == Outcome.DEFER

    def test_context_and_resource_conditions_combined(self):
        """when: [context.task_phase == "review", resource.verb == "git"] → both must hold."""
        rule = self._rule(
            when=['context.task_phase == "review"', 'resource.verb == "git"'],
            reason="git only in review.",
        )
        bc_git = fixture_bash("git push origin main")
        bc_ls = fixture_bash("ls -la")

        # Both hold → DENY
        d = evaluate([rule], "Bash", bc_git, _session(), _ctx(task_phase="review"))
        assert d.outcome == Outcome.DENY

        # context matches but verb doesn't → DEFER
        d = evaluate([rule], "Bash", bc_ls, _session(), _ctx(task_phase="review"))
        assert d.outcome == Outcome.DEFER

        # verb matches but context doesn't → DEFER
        d = evaluate([rule], "Bash", bc_git, _session(), _ctx(task_phase="active"))
        assert d.outcome == Outcome.DEFER


# ---------------------------------------------------------------------------
# Define rule — classifier rules must be skipped by the evaluator
# ---------------------------------------------------------------------------


class TestDefineRule:
    """effect:define rules are classifiers, not enforcement rules.

    The evaluator must skip them entirely so they never produce a decision.
    """

    def _define_rule(self) -> Rule:
        return Rule(
            id="v1:define:secret-file-patterns",
            effect="define",
            resource_type="FilePath",
            define={
                "is_safe_variant": {"patterns": [r"\.(example|sample|template)"]},
                "is_secret": {"patterns": [r"(^|[/\\])\.env(\.[^/\\]+)?$"]},
            },
            tags=["secrets"],
        )

    def test_define_rule_does_not_produce_deny(self):
        """A define rule alone never produces DENY — it produces DEFER."""
        rule = self._define_rule()
        fp = build_file_path_entity("/workspace/src/main.py", secrets_patterns=[])
        d = evaluate([rule], "Read", fp, _session(), {})
        assert d.outcome == Outcome.DEFER

    def test_define_rule_does_not_produce_allow(self):
        """A define rule alone never produces ALLOW — it produces DEFER."""
        rule = self._define_rule()
        fp = build_file_path_entity("/workspace/.env", secrets_patterns=[])
        d = evaluate([rule], "Read", fp, _session(), {})
        assert d.outcome == Outcome.DEFER

    def test_define_rule_coexists_with_enforcement_rules(self):
        """A define rule in a mixed rule list does not interfere with enforcement rules."""
        define = self._define_rule()
        forbid = Rule(
            id="v1:test:deny-all-reads",
            effect="forbid",
            action=["Read"],
            resource_type="FilePath",
            when=[],
            reason="denied",
        )
        fp = build_file_path_entity("/workspace/foo.py", secrets_patterns=[])
        d = evaluate([define, forbid], "Read", fp, _session(), {})
        assert d.outcome == Outcome.DENY
        assert d.rule_id == "v1:test:deny-all-reads"

    def test_define_rule_excluded_from_compound_bash_path(self):
        """define rules don't interfere with the compound-bash structural pre-check."""
        rule = self._define_rule()
        bc = fixture_bash("git status && git diff")
        d = evaluate([rule], "Bash", bc, _session(), {})
        # compound bash is denied by the structural pre-check, not by the define rule
        assert d.outcome == Outcome.DENY
        assert d.rule_id == "builtin:compound-bash"
