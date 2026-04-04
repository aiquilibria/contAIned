"""Synchronous latency benchmark for the Cedar policy engine.

Measures wall-clock time for the operations that fire on every tool call:
  1. Entity building (FilePath, BashCommand, NetworkResource)
  2. evaluate() with the full production rule set
  3. Full pipeline: build + evaluate (what a hook actually executes per call)

Run with:
    pytest tests/engine/test_benchmark.py --benchmark-only -v

The budget assertion on full-pipeline scenarios guards against regressions.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from contained.engine.engine import evaluate
from contained.engine.entities import (
    AgentSession,
    Rule,
    build_bash_command_entity,
    build_file_path_entity,
    build_network_resource_entity,
)

# ---------------------------------------------------------------------------
# p95 budget: hooks fire synchronously per tool call; stay well under this
# ---------------------------------------------------------------------------

LATENCY_BUDGET_US = 5_000  # 5 ms

# ---------------------------------------------------------------------------
# Load production rules (define-effect rules are preprocessed into entities)
# ---------------------------------------------------------------------------

_MANIFEST_PATH = Path(__file__).parents[2] / "docs" / "examples" / "mainlined_v2.yaml"


def _load_rules() -> list[Rule]:
    with _MANIFEST_PATH.open() as fh:
        data = yaml.safe_load(fh)
    raw_rules = data.get("runtime", {}).get("rules", [])
    rules: list[Rule] = []
    for r in raw_rules:
        if r.get("effect") == "define":
            continue
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


_RULES = _load_rules()

# ---------------------------------------------------------------------------
# Shared inputs
# ---------------------------------------------------------------------------

_SESSION = AgentSession(session_id="bench-session")

_SECRETS: list[tuple[str, list[re.Pattern[str]], str]] = [
    ("allow", [re.compile(r"\.(example|sample|template)", re.IGNORECASE)], ""),
    ("block", [re.compile(r"(^|[/\\])\.env(\.[^/\\]+)?$", re.IGNORECASE)], "Secret file."),
]

_ALLOWED_DOMAINS = ["api.anthropic.com", "code.claude.com", "docs.anthropic.com", "github.com"]

# Pre-built entities for evaluate()-only benchmarks (isolate build cost).
_fp_workspace = build_file_path_entity("/workspace/src/main.py", _SECRETS)
_fp_secret    = build_file_path_entity("/workspace/.env",        _SECRETS)
_fp_outside   = build_file_path_entity("/etc/passwd",            _SECRETS)
_bash_git     = build_bash_command_entity("git status",          _SECRETS)
_bash_rm      = build_bash_command_entity("rm -rf /tmp/x",       _SECRETS)
_bash_cd      = build_bash_command_entity("cd /workspace/cli",   _SECRETS)
_net_ok       = build_network_resource_entity("https://api.anthropic.com/v1/messages", _ALLOWED_DOMAINS)
_net_bad      = build_network_resource_entity("https://evil.com/exfil",                _ALLOWED_DOMAINS)

# ---------------------------------------------------------------------------
# Entity-build benchmarks
# ---------------------------------------------------------------------------


def test_build_filepath_workspace(benchmark):
    benchmark.name = "build FilePath (workspace)"
    benchmark(build_file_path_entity, "/workspace/src/main.py", _SECRETS)


def test_build_filepath_secret(benchmark):
    benchmark.name = "build FilePath (.env secret)"
    benchmark(build_file_path_entity, "/workspace/.env", _SECRETS)


def test_build_bash_git(benchmark):
    benchmark.name = "build BashCommand (git status)"
    benchmark(build_bash_command_entity, "git status", _SECRETS)


def test_build_bash_rm(benchmark):
    benchmark.name = "build BashCommand (rm -rf)"
    benchmark(build_bash_command_entity, "rm -rf /tmp/x", _SECRETS)


def test_build_bash_cd(benchmark):
    benchmark.name = "build BashCommand (cd /workspace/cli)"
    benchmark(build_bash_command_entity, "cd /workspace/cli", _SECRETS)


def test_build_network_allowed(benchmark):
    benchmark.name = "build NetworkResource (allowed domain)"
    benchmark(build_network_resource_entity, "https://api.anthropic.com/v1/messages", _ALLOWED_DOMAINS)


def test_build_network_blocked(benchmark):
    benchmark.name = "build NetworkResource (blocked domain)"
    benchmark(build_network_resource_entity, "https://evil.com/exfil", _ALLOWED_DOMAINS)


# ---------------------------------------------------------------------------
# evaluate()-only benchmarks (entity pre-built)
# ---------------------------------------------------------------------------


def test_evaluate_read_workspace(benchmark):
    """Read /workspace/src/main.py → DEFER (no forbid fires, no permit)."""
    benchmark.name = "evaluate Read workspace → DEFER"
    benchmark(evaluate, _RULES, "Read", _fp_workspace, _SESSION, {})


def test_evaluate_read_secret(benchmark):
    """Read .env → DENY (secrets forbid rule)."""
    benchmark.name = "evaluate Read .env → DENY"
    benchmark(evaluate, _RULES, "Read", _fp_secret, _SESSION, {})


def test_evaluate_read_outside(benchmark):
    """Read /etc/passwd → DENY (out-of-workspace forbid rule)."""
    benchmark.name = "evaluate Read /etc/passwd → DENY"
    benchmark(evaluate, _RULES, "Read", _fp_outside, _SESSION, {})


def test_evaluate_bash_git(benchmark):
    """git status → ALLOW (permit-safe-git-reads)."""
    benchmark.name = "evaluate Bash git status → ALLOW"
    benchmark(evaluate, _RULES, "Bash", _bash_git, _SESSION, {})


def test_evaluate_bash_rm(benchmark):
    """rm → DENY (block-destructive)."""
    benchmark.name = "evaluate Bash rm → DENY"
    benchmark(evaluate, _RULES, "Bash", _bash_rm, _SESSION, {})


def test_evaluate_bash_cd(benchmark):
    """cd /workspace/cli → ALLOW (permit-safe-read-only; target_in_workspace)."""
    benchmark.name = "evaluate Bash cd → ALLOW"
    benchmark(evaluate, _RULES, "Bash", _bash_cd, _SESSION, {})


def test_evaluate_webfetch_allowed(benchmark):
    """WebFetch to allowed domain → DEFER."""
    benchmark.name = "evaluate WebFetch allowed → DEFER"
    benchmark(evaluate, _RULES, "WebFetch", _net_ok, _SESSION, {})


def test_evaluate_webfetch_blocked(benchmark):
    """WebFetch to blocked domain → DENY."""
    benchmark.name = "evaluate WebFetch blocked → DENY"
    benchmark(evaluate, _RULES, "WebFetch", _net_bad, _SESSION, {})


# ---------------------------------------------------------------------------
# Full-pipeline benchmarks (build + evaluate — what a hook actually does)
# ---------------------------------------------------------------------------


def test_pipeline_read_workspace(benchmark):
    benchmark.name = "pipeline Read workspace file"

    def _run():
        e = build_file_path_entity("/workspace/src/main.py", _SECRETS)
        evaluate(_RULES, "Read", e, _SESSION, {})

    result = benchmark(_run)
    assert benchmark.stats["mean"] * 1e6 < LATENCY_BUDGET_US, (
        f"mean latency {benchmark.stats['mean'] * 1e6:.1f}µs exceeds {LATENCY_BUDGET_US}µs budget"
    )
    return result


def test_pipeline_read_secret(benchmark):
    benchmark.name = "pipeline Read .env → DENY"

    def _run():
        e = build_file_path_entity("/workspace/.env", _SECRETS)
        evaluate(_RULES, "Read", e, _SESSION, {})

    result = benchmark(_run)
    assert benchmark.stats["mean"] * 1e6 < LATENCY_BUDGET_US
    return result


def test_pipeline_bash_git(benchmark):
    benchmark.name = "pipeline Bash git status → ALLOW"

    def _run():
        e = build_bash_command_entity("git status", _SECRETS)
        evaluate(_RULES, "Bash", e, _SESSION, {})

    result = benchmark(_run)
    assert benchmark.stats["mean"] * 1e6 < LATENCY_BUDGET_US
    return result


def test_pipeline_bash_rm(benchmark):
    benchmark.name = "pipeline Bash rm → DENY"

    def _run():
        e = build_bash_command_entity("rm -rf /tmp/x", _SECRETS)
        evaluate(_RULES, "Bash", e, _SESSION, {})

    result = benchmark(_run)
    assert benchmark.stats["mean"] * 1e6 < LATENCY_BUDGET_US
    return result


def test_pipeline_webfetch_blocked(benchmark):
    benchmark.name = "pipeline WebFetch blocked → DENY"

    def _run():
        e = build_network_resource_entity("https://evil.com/exfil", _ALLOWED_DOMAINS)
        evaluate(_RULES, "WebFetch", e, _SESSION, {})

    result = benchmark(_run)
    assert benchmark.stats["mean"] * 1e6 < LATENCY_BUDGET_US
    return result
