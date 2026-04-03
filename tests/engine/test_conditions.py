"""Tests for the condition parser and evaluator."""

from __future__ import annotations

import pytest

from contained.engine.conditions import evaluate_condition
from contained.engine.entities import (
    AgentSession,
    BashCommand,
    FilePath,
    build_bash_command_entity,
    build_file_path_entity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(**kwargs) -> AgentSession:
    return AgentSession(session_id="test-session", **kwargs)


def _fp(path: str, **kwargs) -> FilePath:
    return build_file_path_entity(path, secrets_patterns=[], **kwargs)


def _bash(cmd: str) -> BashCommand:
    return build_bash_command_entity(cmd, secrets_patterns=[])


# ---------------------------------------------------------------------------
# == operator
# ---------------------------------------------------------------------------


class TestEquality:
    def test_string_match(self):
        bc = _bash("git push")
        assert evaluate_condition('resource.verb == "git"', bc, _session(), {}) is True

    def test_string_no_match(self):
        bc = _bash("go build")
        assert evaluate_condition('resource.verb == "git"', bc, _session(), {}) is False

    def test_bool_true(self):
        import re

        patterns = [("block", [re.compile(r"\.env$", re.IGNORECASE)], "")]
        fp = build_file_path_entity("/workspace/.env", secrets_patterns=patterns)
        assert evaluate_condition("resource.is_secret == true", fp, _session(), {}) is True

    def test_bool_false(self):
        fp = _fp("/workspace/main.py")
        assert evaluate_condition("resource.is_secret == false", fp, _session(), {}) is True

    def test_int_match(self):
        s = _session(tool_call_count=5)
        assert evaluate_condition("principal.tool_call_count == 5", _fp("/w"), s, {}) is True

    def test_null_literal(self):
        bc = _bash("ls")
        assert evaluate_condition("resource.subcommand == null", bc, _session(), {}) is True


# ---------------------------------------------------------------------------
# != operator
# ---------------------------------------------------------------------------


class TestInequality:
    def test_not_equal(self):
        bc = _bash("go build")
        assert evaluate_condition('resource.verb != "git"', bc, _session(), {}) is True

    def test_equal_not_matching(self):
        bc = _bash("git push")
        assert evaluate_condition('resource.verb != "git"', bc, _session(), {}) is False


# ---------------------------------------------------------------------------
# in / not in
# ---------------------------------------------------------------------------


class TestIn:
    def test_attr_in_list(self):
        bc = _bash("git push")
        cond = 'resource.verb in ["git", "go"]'
        assert evaluate_condition(cond, bc, _session(), {}) is True

    def test_attr_not_in_list(self):
        bc = _bash("rm -rf /")
        cond = 'resource.verb in ["git", "go"]'
        assert evaluate_condition(cond, bc, _session(), {}) is False

    def test_literal_in_list_attr(self):
        bc = _bash("git push --force")
        cond = '"--force" in resource.args'
        assert evaluate_condition(cond, bc, _session(), {}) is True

    def test_literal_not_in_list_attr(self):
        bc = _bash("git push")
        cond = '"--force" in resource.args'
        assert evaluate_condition(cond, bc, _session(), {}) is False

    def test_not_in(self):
        bc = _bash("rm -rf /")
        cond = 'resource.verb not in ["git", "go"]'
        assert evaluate_condition(cond, bc, _session(), {}) is True

    def test_not_in_false(self):
        bc = _bash("git push")
        cond = 'resource.verb not in ["git", "go"]'
        assert evaluate_condition(cond, bc, _session(), {}) is False


# ---------------------------------------------------------------------------
# contains
# ---------------------------------------------------------------------------


class TestContains:
    def test_contains_present(self):
        bc = _bash("git push --force")
        assert evaluate_condition('resource.args contains "--force"', bc, _session(), {}) is True

    def test_contains_absent(self):
        bc = _bash("git push")
        assert evaluate_condition('resource.args contains "--force"', bc, _session(), {}) is False

    def test_contains_non_list_raises(self):
        bc = _bash("git push")
        with pytest.raises(TypeError, match="'contains' operator requires a list"):
            evaluate_condition('resource.verb contains "git"', bc, _session(), {})


# ---------------------------------------------------------------------------
# matches / not matches (fnmatch glob)
# ---------------------------------------------------------------------------


class TestMatches:
    def test_matches_exact(self):
        fp = _fp("/workspace/src/foo.py")
        # extension is ".py"; ".py" matches ".py" exactly
        assert evaluate_condition('resource.extension matches ".py"', fp, _session(), {}) is True

    def test_matches_glob_star(self):
        fp = _fp("/workspace/src/foo.py")
        # fnmatch: "*.py" matches ".py" (Python fnmatch does not use shell hidden-file rules)
        assert evaluate_condition('resource.extension matches "*.py"', fp, _session(), {}) is True

    def test_matches_wildcard(self):
        fp = _fp("/workspace/src/foo.py")
        assert evaluate_condition('resource.extension matches ".*"', fp, _session(), {}) is True

    def test_not_matches(self):
        fp = _fp("/workspace/src/foo.py")
        result = evaluate_condition('resource.extension not matches ".go"', fp, _session(), {})
        assert result is True

    def test_matches_non_string_raises(self):
        fp = _fp("/workspace/foo.py")
        with pytest.raises(TypeError, match="'matches' requires a string attribute"):
            evaluate_condition('resource.is_secret matches "true"', fp, _session(), {})


# ---------------------------------------------------------------------------
# matches_re (Phase 1 compat)
# ---------------------------------------------------------------------------


class TestMatchesRe:
    def test_matches_re_basic(self):
        bc = _bash("git status")
        result = evaluate_condition(r"resource.raw matches_re '^git\s+status'", bc, _session(), {})
        assert result is True

    def test_matches_re_no_match(self):
        bc = _bash("go build")
        result = evaluate_condition(r"resource.raw matches_re '^git\s+status'", bc, _session(), {})
        assert result is False


# ---------------------------------------------------------------------------
# Numeric comparisons
# ---------------------------------------------------------------------------


class TestNumericComparisons:
    def test_greater_than(self):
        s = _session(tool_call_count=10)
        assert evaluate_condition("principal.tool_call_count > 5", _fp("/w"), s, {}) is True

    def test_greater_than_equal(self):
        s = _session(tool_call_count=5)
        assert evaluate_condition("principal.tool_call_count >= 5", _fp("/w"), s, {}) is True

    def test_less_than(self):
        s = _session(tool_call_count=3)
        assert evaluate_condition("principal.tool_call_count < 5", _fp("/w"), s, {}) is True

    def test_less_than_equal(self):
        s = _session(tool_call_count=5)
        assert evaluate_condition("principal.tool_call_count <= 5", _fp("/w"), s, {}) is True

    def test_not_greater(self):
        s = _session(tool_call_count=3)
        assert evaluate_condition("principal.tool_call_count > 5", _fp("/w"), s, {}) is False


# ---------------------------------------------------------------------------
# is null / is not null
# ---------------------------------------------------------------------------


class TestNullChecks:
    def test_is_null_none(self):
        bc = _bash("ls")
        assert evaluate_condition("resource.subcommand is null", bc, _session(), {}) is True

    def test_is_null_not_null(self):
        bc = _bash("git push")
        assert evaluate_condition("resource.subcommand is null", bc, _session(), {}) is False

    def test_is_not_null(self):
        bc = _bash("git push")
        assert evaluate_condition("resource.subcommand is not null", bc, _session(), {}) is True

    def test_is_not_null_when_null(self):
        bc = _bash("ls")
        assert evaluate_condition("resource.subcommand is not null", bc, _session(), {}) is False


# ---------------------------------------------------------------------------
# context.* attributes
# ---------------------------------------------------------------------------


class TestContextAttributes:
    def test_context_attr_present(self):
        fp = _fp("/workspace/foo.py")
        ctx = {"task_phase": "review"}
        assert evaluate_condition('context.task_phase == "review"', fp, _session(), ctx) is True

    def test_context_attr_absent_raises_attribute_error(self):
        fp = _fp("/workspace/foo.py")
        with pytest.raises(AttributeError, match="context.task_phase"):
            evaluate_condition('context.task_phase == "review"', fp, _session(), {})


# ---------------------------------------------------------------------------
# AttributeError propagation for unknown attributes
# ---------------------------------------------------------------------------


class TestAttributeErrorPropagation:
    def test_unknown_resource_attr_raises(self):
        fp = _fp("/workspace/foo.py")
        with pytest.raises(AttributeError):
            evaluate_condition("resource.nonexistent_field == true", fp, _session(), {})

    def test_unknown_principal_attr_raises(self):
        fp = _fp("/workspace/foo.py")
        with pytest.raises(AttributeError):
            evaluate_condition("principal.nonexistent_field == true", fp, _session(), {})


# ---------------------------------------------------------------------------
# Syntax errors
# ---------------------------------------------------------------------------


class TestSyntaxErrors:
    def test_unrecognised_syntax(self):
        with pytest.raises(ValueError, match="Unrecognised condition syntax"):
            evaluate_condition('resource.verb XYZOP "git"', _fp("/w"), _session(), {})

    def test_invalid_namespace(self):
        with pytest.raises(ValueError, match="Unknown namespace"):
            evaluate_condition('other.foo == "bar"', _fp("/w"), _session(), {})

    def test_missing_dot_notation(self):
        with pytest.raises(ValueError, match="Invalid attribute reference"):
            evaluate_condition('resourceverb == "git"', _fp("/w"), _session(), {})

    def test_in_rhs_not_list_raises(self):
        bc = _bash("git push")
        with pytest.raises(TypeError, match="'in' RHS must be a list"):
            evaluate_condition('resource.verb in "git"', bc, _session(), {})
