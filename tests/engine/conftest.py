"""Shared fixtures for the engine test suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from contained.engine.engine import evaluate
from contained.engine.entities import (
    AgentSession,
    BashCommand,
    Decision,
    FilePath,
    GlobPattern,
    NetworkResource,
    Rule,
    build_bash_command_entity,
    build_file_path_entity,
    build_glob_pattern_entity,
    build_network_resource_entity,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Principal fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> AgentSession:
    """A default AgentSession for use in engine tests."""
    return AgentSession(session_id="test-session-001")


# ---------------------------------------------------------------------------
# Secrets patterns (empty — tests supply their own patterns as needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_secrets() -> list:
    """Empty secrets patterns list — no files are treated as secrets."""
    return []


@pytest.fixture
def default_secrets() -> list:
    """Minimal secrets patterns: dotenv files are secret, .example variants are safe."""
    import re

    return [
        ("allow", [re.compile(r"\.(example|sample|template)", re.IGNORECASE)], ""),
        (
            "block",
            [re.compile(r"(^|[/\\])\.env(\.[^/\\]+)?$", re.IGNORECASE)],
            "Secret files (credentials, keys, .env) may not be accessed.",
        ),
    ]


# ---------------------------------------------------------------------------
# Entity builder helpers
# ---------------------------------------------------------------------------


def fixture_file_path(
    raw_path: str,
    secrets_patterns: list | None = None,
) -> FilePath:
    """Build a FilePath entity from a raw path string.

    A convenience wrapper around build_file_path_entity for use in tests.
    Defaults to no secrets patterns; pass *secrets_patterns* to test
    is_secret / is_safe_variant behaviour.
    """
    return build_file_path_entity(raw_path, secrets_patterns=secrets_patterns or [])


def fixture_glob_pattern(pattern: str) -> GlobPattern:
    """Build a GlobPattern entity from a raw glob pattern string."""
    return build_glob_pattern_entity(pattern)


def fixture_bash(command: str, secrets_patterns: list | None = None) -> BashCommand:
    """Build a BashCommand entity from a raw command string."""
    return build_bash_command_entity(command, secrets_patterns=secrets_patterns or [])


def fixture_network(url: str, allowed_domains: list[str] | None = None) -> NetworkResource:
    """Build a NetworkResource entity from a URL."""
    return build_network_resource_entity(url, allowed_domains=allowed_domains or [])


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------


def load_fixture_rules(filename: str = "rules_default.yaml") -> list[Rule]:
    """Load Rule objects from a fixture YAML file in tests/engine/fixtures/."""
    path = FIXTURES_DIR / filename
    with path.open() as fh:
        data = yaml.safe_load(fh)
    rules = []
    for r in data.get("rules", []):
        action = r.get("action", [])
        if isinstance(action, str):
            action = [action]
        rules.append(
            Rule(
                id=r["id"],
                effect=r["effect"],
                action=action,
                resource_type=r.get("resource_type", "*"),
                when=r.get("when", []),
                unless=r.get("unless", []),
                reason=r.get("reason"),
                tags=r.get("tags", []),
            )
        )
    return rules


# ---------------------------------------------------------------------------
# Decision helper
# ---------------------------------------------------------------------------


def make_decision(
    rules: list[Rule],
    action: str,
    resource_entity: Any,
    principal: AgentSession,
    context: dict[str, Any] | None = None,
) -> Decision:
    """Thin wrapper around evaluate() for use in test assertions."""
    return evaluate(rules, action, resource_entity, principal, context=context or {})
