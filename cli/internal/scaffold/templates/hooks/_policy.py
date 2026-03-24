#!/usr/bin/env python3
"""Shared policy loader for contAIned hooks.

Reads the policy: section from /etc/contained/manifest.yaml (baked into the
container image by contAIned init) and merges it with structural defaults so
that every key is always present.  The manifest is the sole source of rule
data — if it cannot be read, hooks receive empty pattern lists and apply no
secret-file checks.

Manifest location:
  /etc/contained/manifest.yaml  — baked into the image by contAIned init

Action values: "block" | "allow"
  block    — deny the operation; hook exits 2 with a reason on stderr
  allow    — permit unconditionally
"""
from pathlib import Path

_DEFAULTS = {
    "secrets": {
        "rules": [],
    },
    "bash": {
        "rules": [],
    },
    "audit": {
        "enabled": True,
        "jsonl_export": False,
    },
    "qa": {
        "checks": [],
    },
    "network": {
        "enabled":        False,
        "allowed_domains": [
            "api.anthropic.com",
            "code.claude.com",
            "docs.anthropic.com",
        ],
    },
    "mcp": {
        "approved_servers": [],
    },
    "skills": {
        "approved_skills": [],
    },
}


def _deep_merge(base, override):
    """Recursively merge *override* into *base*, returning a new dict."""
    result = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _compile_patterns(policy):
    """Compile all manifest pattern strings into regex objects in-place.

    Populates:
      policy["secrets"]["_compiled_rules"] — list[(action, [re.Pattern], reason)]
      policy["bash"]["_compiled_rules"]    — list[(action, [re.Pattern], reason)]

    Called once per load_policy() invocation.  All rule data comes
    exclusively from the manifest; there are no hardcoded built-ins.
    First-match-wins: list allow rules before block rules in the manifest.
    """
    import re as _re

    def _compile_rules(rules, flags=0):
        return [
            (
                rule["action"],
                [_re.compile(p, flags) for p in rule.get("patterns", [])],
                rule.get("reason", ""),
            )
            for rule in rules
        ]

    policy["secrets"]["_compiled_rules"] = _compile_rules(
        policy["secrets"].get("rules", []), _re.IGNORECASE
    )
    policy["bash"]["_compiled_rules"] = _compile_rules(
        policy["bash"].get("rules", [])
    )


def load_policy(cwd="."):
    """Return the fully-merged policy dict with all patterns pre-compiled.

    Always succeeds: if the manifest is unreadable the structural defaults
    are returned (empty pattern lists — no checks applied).
    """
    try:
        import yaml
        manifest_path = Path("/etc/contained/manifest.yaml")
        with manifest_path.open() as fh:
            manifest = yaml.safe_load(fh) or {}
        policy = _deep_merge(_DEFAULTS, manifest.get("policy", {}))
    except Exception:
        policy = dict(_DEFAULTS)
    _compile_patterns(policy)
    return policy


