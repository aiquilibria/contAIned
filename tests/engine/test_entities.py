"""Tests for engine entity models and builder functions."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from contained.engine.entities import (
    CONTEXT_SCHEMA,
    BashCommand,
    Decision,
    FilePath,
    GlobPattern,
    Outcome,
    Rule,
    build_bash_command_entity,
    build_context,
    build_file_path_entity,
    build_glob_pattern_entity,
    extract_file_targets,
    is_glob_tool,
)
from contained.engine.validator import ValidationResult, validate_rules

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOTENV_PATTERNS = [
    ("allow", [re.compile(r"\.(example|sample|template)", re.IGNORECASE)], ""),
    (
        "block",
        [re.compile(r"(^|[/\\])\.env(\.[^/\\]+)?$", re.IGNORECASE)],
        "Secret files may not be accessed.",
    ),
]


def _fp(path: str, patterns=None) -> FilePath:
    return build_file_path_entity(path, secrets_patterns=patterns or [])


def _bash(cmd: str, patterns=None) -> BashCommand:
    return build_bash_command_entity(cmd, secrets_patterns=patterns or [])


# ---------------------------------------------------------------------------
# FilePath — model validator
# ---------------------------------------------------------------------------


class TestFilePathValidator:
    def test_safe_variant_requires_secret(self):
        with pytest.raises(ValidationError, match="is_safe_variant=True requires is_secret=True"):
            FilePath(
                raw_path=".env.example",
                normalized="/workspace/.env.example",
                in_workspace=True,
                in_tmp=False,
                is_secret=False,
                is_safe_variant=True,  # invalid: can't be safe variant without is_secret
                in_control_plane=False,
                extension=".example",
                relative_path=".env.example",
            )

    def test_safe_variant_with_secret_is_valid(self):
        fp = FilePath(
            raw_path=".env.example",
            normalized="/workspace/.env.example",
            in_workspace=True,
            in_tmp=False,
            is_secret=True,
            is_safe_variant=True,
            in_control_plane=False,
            extension=".example",
            relative_path=".env.example",
        )
        assert fp.is_safe_variant is True

    def test_secret_without_safe_variant_is_valid(self):
        fp = FilePath(
            raw_path=".env",
            normalized="/workspace/.env",
            in_workspace=True,
            in_tmp=False,
            is_secret=True,
            is_safe_variant=False,
            in_control_plane=False,
            extension=None,
            relative_path=".env",
        )
        assert fp.is_secret is True
        assert fp.is_safe_variant is False

    def test_frozen(self):
        fp = _fp("/workspace/foo.py")
        with pytest.raises(Exception):
            fp.raw_path = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FilePath — builder: secret detection
# ---------------------------------------------------------------------------


class TestBuildFilePathEntity:
    def test_non_secret(self):
        fp = _fp("/workspace/src/main.py", _DOTENV_PATTERNS)
        assert fp.is_secret is False
        assert fp.is_safe_variant is False

    def test_dotenv_is_secret(self):
        fp = _fp("/workspace/.env", _DOTENV_PATTERNS)
        assert fp.is_secret is True
        assert fp.is_safe_variant is False

    def test_dotenv_example_is_safe_variant(self):
        fp = _fp("/workspace/.env.example", _DOTENV_PATTERNS)
        assert fp.is_secret is True
        assert fp.is_safe_variant is True

    def test_in_workspace(self):
        fp = _fp("/workspace/foo.py")
        assert fp.in_workspace is True

    def test_outside_workspace(self):
        fp = _fp("/etc/passwd")
        assert fp.in_workspace is False

    def test_control_plane_detection(self):
        fp = _fp("/workspace/.contAIned/hooks/qa.py")
        assert fp.in_control_plane is True

    def test_non_control_plane(self):
        fp = _fp("/workspace/src/main.py")
        assert fp.in_control_plane is False

    def test_extension(self):
        fp = _fp("/workspace/foo.PY")
        assert fp.extension == ".py"  # lowercased

    def test_no_extension(self):
        fp = _fp("/workspace/Makefile")
        assert fp.extension is None

    def test_relative_path(self):
        fp = _fp("src/main.py")
        assert fp.raw_path == "src/main.py"


# ---------------------------------------------------------------------------
# GlobPattern — builder
# ---------------------------------------------------------------------------


class TestBuildGlobPatternEntity:
    def test_wildcard_star_star(self):
        gp = build_glob_pattern_entity("**/*.py")
        assert gp.pattern == "**/*.py"
        assert gp.prefix_path is None
        assert gp.in_workspace is False

    def test_relative_prefix(self):
        gp = build_glob_pattern_entity("src/**/*.py")
        assert gp.prefix_path == "src"
        assert gp.in_workspace is True  # relative → workspace-relative

    def test_absolute_workspace_prefix(self):
        gp = build_glob_pattern_entity("/workspace/src/*.py")
        assert gp.prefix_path == "/workspace/src"
        assert gp.in_workspace is True

    def test_absolute_outside_workspace(self):
        gp = build_glob_pattern_entity("/etc/*.conf")
        assert gp.prefix_path == "/etc"
        assert gp.in_workspace is False

    def test_literal_no_wildcards(self):
        gp = build_glob_pattern_entity("/workspace/foo.py")
        assert gp.prefix_path == "/workspace/foo.py"
        assert gp.in_workspace is True

    def test_wildcard_at_start(self):
        gp = build_glob_pattern_entity("*.py")
        assert gp.prefix_path is None
        assert gp.in_workspace is False

    def test_frozen(self):
        gp = build_glob_pattern_entity("**/*.py")
        with pytest.raises(Exception):
            gp.pattern = "other"  # type: ignore[misc]

    def test_returns_glob_pattern_type(self):
        gp = build_glob_pattern_entity("src/**")
        assert isinstance(gp, GlobPattern)


# ---------------------------------------------------------------------------
# BashCommand — model validator
# ---------------------------------------------------------------------------


class TestBashCommandValidator:
    def test_secret_target_requires_path(self):
        with pytest.raises(ValidationError, match="target_is_secret=True requires target_path"):
            BashCommand(
                raw="cat .env",
                verb="cat",
                subcommand=None,
                args=[],
                positional_args=[".env"],
                target_path=None,  # invalid: target_is_secret requires target_path
                target_is_secret=True,
                target_in_workspace=False,
                target_in_tmp=False,
                is_compound=False,
            )

    def test_frozen(self):
        bc = _bash("git status")
        with pytest.raises(Exception):
            bc.verb = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BashCommand — builder: token decomposition
# ---------------------------------------------------------------------------


class TestBuildBashCommandEntity:
    def test_verb_only(self):
        bc = _bash("ls")
        assert bc.verb == "ls"
        assert bc.subcommand is None
        assert bc.args == []
        assert bc.positional_args == []

    def test_verb_and_subcommand(self):
        bc = _bash("git push")
        assert bc.verb == "git"
        assert bc.subcommand == "push"

    def test_flags_in_args(self):
        bc = _bash("git push --force --dry-run")
        assert "--force" in bc.args
        assert "--dry-run" in bc.args

    def test_positional_args(self):
        bc = _bash("git push origin main")
        assert "origin" in bc.positional_args
        assert "main" in bc.positional_args

    def test_flag_value_not_in_positional_args(self):
        # --mainlined http://... : the URL should be consumed as a kwarg value
        # and NOT appear in positional_args.
        bc = _bash("contained --mainlined http://mainlined.example.com")
        assert "http://mainlined.example.com" not in bc.positional_args

    def test_is_compound_ampersand(self):
        bc = _bash("git status && git diff")
        assert bc.is_compound is True

    def test_is_compound_pipe(self):
        bc = _bash("cat foo | grep bar")
        assert bc.is_compound is True

    def test_is_compound_semicolon(self):
        bc = _bash("cd /tmp; ls")
        assert bc.is_compound is True

    def test_is_compound_or(self):
        bc = _bash("false || echo ok")
        assert bc.is_compound is True

    def test_not_compound_simple(self):
        bc = _bash("git push origin main")
        assert bc.is_compound is False

    def test_quoted_ampersand_not_compound(self):
        # && inside a quoted argument is not a shell operator.
        bc = _bash('echo "hello && world"')
        assert bc.is_compound is False

    def test_target_path_extraction(self):
        bc = _bash("cat /workspace/src/main.py")
        assert bc.target_path == "/workspace/src/main.py"

    def test_no_target_path(self):
        bc = _bash("git status")
        assert bc.target_path is None

    def test_target_is_secret(self):
        bc = _bash("cat /workspace/.env", _DOTENV_PATTERNS)
        assert bc.target_is_secret is True
        assert bc.target_path == "/workspace/.env"

    def test_target_not_secret(self):
        bc = _bash("cat /workspace/src/main.py", _DOTENV_PATTERNS)
        assert bc.target_is_secret is False


# ---------------------------------------------------------------------------
# BashCommand — is_compound: shlex-based detection
# ---------------------------------------------------------------------------


class TestIsCompound:
    """Detailed tests for shlex-based compound detection edge cases."""

    def test_double_ampersand(self):
        assert _bash("a && b").is_compound is True

    def test_double_pipe(self):
        assert _bash("a || b").is_compound is True

    def test_single_pipe(self):
        assert _bash("a | b").is_compound is True

    def test_semicolon(self):
        assert _bash("a; b").is_compound is True

    def test_quoted_pipe_not_compound(self):
        assert _bash('echo "a | b"').is_compound is False

    def test_single_command_not_compound(self):
        assert _bash("pytest tests/").is_compound is False


# ---------------------------------------------------------------------------
# extract_file_targets
# ---------------------------------------------------------------------------


class TestExtractFileTargets:
    def test_read(self):
        targets = extract_file_targets("Read", {"file_path": "/workspace/foo.py"})
        assert targets == ["/workspace/foo.py"]

    def test_write(self):
        targets = extract_file_targets("Write", {"file_path": "/workspace/foo.py"})
        assert targets == ["/workspace/foo.py"]

    def test_edit(self):
        targets = extract_file_targets("Edit", {"file_path": "/workspace/foo.py"})
        assert targets == ["/workspace/foo.py"]

    def test_multiedit(self):
        tool_input = {
            "edits": [
                {"file_path": "/workspace/a.py"},
                {"file_path": "/workspace/b.py"},
            ]
        }
        result = extract_file_targets("MultiEdit", tool_input)
        assert result == ["/workspace/a.py", "/workspace/b.py"]

    def test_multiedit_empty(self):
        assert extract_file_targets("MultiEdit", {"edits": []}) == []

    def test_glob(self):
        assert extract_file_targets("Glob", {"pattern": "**/*.py"}) == ["**/*.py"]

    def test_grep(self):
        assert extract_file_targets("Grep", {"path": "/workspace/src"}) == ["/workspace/src"]

    def test_bash_returns_empty(self):
        assert extract_file_targets("Bash", {"command": "ls"}) == []

    def test_missing_path_returns_empty(self):
        assert extract_file_targets("Read", {}) == []


# ---------------------------------------------------------------------------
# is_glob_tool
# ---------------------------------------------------------------------------


class TestIsGlobTool:
    def test_glob_is_glob(self):
        assert is_glob_tool("Glob") is True

    def test_read_is_not_glob(self):
        assert is_glob_tool("Read") is False

    def test_bash_is_not_glob(self):
        assert is_glob_tool("Bash") is False


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


class TestDecision:
    def test_rule_version_v0(self):
        d = Decision(outcome=Outcome.DENY, rule_id="v0:secrets:dotenv", reason="x")
        assert d.rule_version == "v0"

    def test_rule_version_builtin(self):
        d = Decision(outcome=Outcome.DENY, rule_id="builtin:compound-bash", reason="x")
        assert d.rule_version == "builtin"

    def test_rule_version_none_on_defer(self):
        d = Decision(outcome=Outcome.DEFER)
        assert d.rule_version is None

    def test_frozen(self):
        d = Decision(outcome=Outcome.ALLOW, rule_id="v1:test:rule")
        with pytest.raises(Exception):
            d.outcome = Outcome.DENY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CONTEXT_SCHEMA and build_context (Phase 3)
# ---------------------------------------------------------------------------


class TestContextSchema:
    def test_schema_has_expected_keys(self):
        assert "task_phase" in CONTEXT_SCHEMA
        assert "qa_status" in CONTEXT_SCHEMA
        assert "tool_call_count" in CONTEXT_SCHEMA

    def test_tool_call_count_is_int(self):
        assert CONTEXT_SCHEMA["tool_call_count"] is int


class TestBuildContext:
    def test_returns_dict_with_all_keys(self):
        ctx = build_context({})
        assert "task_phase" in ctx
        assert "qa_status" in ctx
        assert "tool_call_count" in ctx

    def test_defaults_when_no_db(self, tmp_path):
        ctx = build_context({"cwd": str(tmp_path), "session_id": "test-session"})
        assert ctx["task_phase"] in ("active", "initialization", "qa", "review")
        assert ctx["qa_status"] in ("passing", "failing", "not_run", "unknown")
        assert isinstance(ctx["tool_call_count"], int)

    def test_env_task_phase_override(self, monkeypatch):
        monkeypatch.setenv("CONTAINED_TASK_PHASE", "review")
        ctx = build_context({})
        assert ctx["task_phase"] == "review"

    def test_env_task_phase_default(self, monkeypatch):
        monkeypatch.delenv("CONTAINED_TASK_PHASE", raising=False)
        ctx = build_context({})
        assert ctx["task_phase"] == "active"

    def test_graceful_on_missing_session_id(self):
        ctx = build_context({"cwd": "/workspace"})
        assert ctx["tool_call_count"] == 0

    def test_graceful_on_nonexistent_cwd(self, monkeypatch):
        monkeypatch.delenv("CONTAINED_TASK_PHASE", raising=False)
        ctx = build_context({"cwd": "/nonexistent/path", "session_id": "s1"})
        assert ctx["task_phase"] == "active"
        assert ctx["qa_status"] == "unknown"
        assert ctx["tool_call_count"] == 0


# ---------------------------------------------------------------------------
# Rule — effect: define
# ---------------------------------------------------------------------------


class TestDefineRule:
    def test_define_rule_is_valid(self):
        rule = Rule(
            id="v1:define:secrets",
            effect="define",
            define={
                "is_safe_variant": {"patterns": [r"\.(example|sample|template)"]},
                "is_secret": {"patterns": [r"(^|[/\\])\.env(\.[^/\\]+)?$"]},
            },
        )
        assert rule.effect == "define"
        assert rule.define is not None
        assert "is_secret" in rule.define

    def test_define_rule_defaults(self):
        """action and resource_type should have safe defaults for define rules."""
        rule = Rule(
            id="v1:define:foo",
            effect="define",
            define={"is_secret": {"patterns": [r"\.env$"]}},
        )
        assert rule.action == []
        assert rule.resource_type == "*"

    def test_define_rule_is_frozen(self):
        rule = Rule(
            id="v1:define:foo",
            effect="define",
            define={"is_secret": {"patterns": []}},
        )
        with pytest.raises(Exception):
            rule.define = {}  # type: ignore[misc]

    def test_define_rule_has_no_enforcement_semantics(self):
        """define rules carry no when/unless/reason — those fields remain at default."""
        rule = Rule(
            id="v1:define:foo",
            effect="define",
            define={"is_secret": {"patterns": [r"\.env$"]}},
        )
        assert rule.when == []
        assert rule.unless == []
        assert rule.reason is None


# ---------------------------------------------------------------------------
# Validator — define rule checks
# ---------------------------------------------------------------------------


class TestValidatorDefineRule:
    def test_valid_define_rule_produces_no_errors(self):
        rule = Rule(
            id="v1:define:secrets",
            effect="define",
            resource_type="FilePath",
            define={
                "is_safe_variant": {"patterns": [r"\.(example|sample|template)"]},
                "is_secret": {"patterns": [r"(^|[/\\])\.env(\.[^/\\]+)?$"]},
            },
        )
        result = validate_rules([rule], phase=2)
        assert result.errors == []

    def test_define_rule_without_define_block_is_error(self):
        rule = Rule(
            id="v1:define:bad",
            effect="define",
            define=None,
        )
        result = validate_rules([rule], phase=2)
        assert any("non-empty 'define' block" in i.message for i in result.errors)

    def test_define_rule_does_not_require_action(self):
        """Validator must not flag missing action on a define rule."""
        rule = Rule(
            id="v1:define:ok",
            effect="define",
            action=[],
            define={"is_secret": {"patterns": [r"\.env$"]}},
        )
        result = validate_rules([rule], phase=2)
        assert result.errors == []

    def test_define_rule_not_flagged_for_logical_consistency(self):
        """A define rule alongside a forbid rule with the same resource_type must not
        trigger the dead-code consistency warning."""
        define = Rule(
            id="v1:define:secrets",
            effect="define",
            resource_type="FilePath",
            define={"is_secret": {"patterns": [r"\.env$"]}},
        )
        forbid = Rule(
            id="v1:secrets:block",
            effect="forbid",
            action=["Read"],
            resource_type="FilePath",
            when=["resource.is_secret == true"],
        )
        result: ValidationResult = validate_rules([define, forbid], phase=2)
        assert result.warnings == []
        assert result.errors == []
