"""Policy loader for the Cedar-inspired engine.

Reads /etc/contained/manifest.yaml (baked in by contAIned init) and
returns Rule objects and compiled secrets patterns for use by the engine.

Two public functions:
  load_secrets_patterns() — compiled regex patterns for entity builders.
  load_rules()            — Rule list for engine evaluation.

Manifest location:
  /etc/contained/manifest.yaml   — primary (baked into image by contAIned init)
  /workspace/.contAIned/manifest.yaml — fallback for development / testing

Compat adapter (Phase 1):
  If policy.rules is absent, translates legacy bash/secrets/network sections
  into Rule objects with v0: prefix IDs so the engine can be used immediately
  without migrating the manifest schema.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from contained.engine.entities import Rule

_MANIFEST_PATH = Path("/etc/contained/manifest.yaml")
_WORKSPACE_MANIFEST_PATH = Path("/workspace/.contAIned/manifest.yaml")

# Tool groups used when constructing compat rules.
_FILE_ACTIONS = ["Read", "Write", "Edit", "MultiEdit", "Glob", "Grep"]
_NETWORK_ACTIONS = ["WebFetch", "WebSearch"]

_DEFAULTS: dict[str, Any] = {
    "secrets": {"rules": []},
    "bash": {"rules": []},
    "network": {
        "enabled": False,
        "allowed_domains": [
            "api.anthropic.com",
            "code.claude.com",
            "docs.anthropic.com",
        ],
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_secrets_patterns() -> list[tuple[str, list, str]]:
    """Return compiled regex patterns for secret-file detection.

    Returns a list of (action, [compiled_re, ...], reason) tuples where
    action is "allow" (safe variant) or "block" (secret).  Used by entity
    builders to compute is_secret and is_safe_variant on each entity.

    First-match-wins: allow (safe-variant) patterns must appear before block
    (secret) patterns.  define rules enforce this ordering automatically by
    processing is_safe_variant before is_secret.

    Source priority:
      1. effect:define rules in policy.rules (Phase 2+ format).
      2. Legacy policy.secrets.rules (Phase 1 compat).
    """
    policy = _load_policy()

    # Prefer define rules in policy.rules (Phase 2+ format).
    if "rules" in policy:
        define_patterns = _extract_define_patterns(policy["rules"])
        if define_patterns:
            return define_patterns

    # Fall back to legacy policy.secrets.rules.
    result: list[tuple[str, list, str]] = []
    for rule in policy.get("secrets", {}).get("rules", []):
        compiled = [re.compile(p, re.IGNORECASE) for p in rule.get("patterns", [])]
        result.append((rule["action"], compiled, rule.get("reason", "")))
    return result


def _extract_define_patterns(raw_rules: list[dict]) -> list[tuple[str, list, str]]:
    """Extract compiled secret patterns from effect:define rules targeting FilePath.

    Processes is_safe_variant before is_secret to preserve first-match-wins
    semantics in the entity builder.
    """
    result: list[tuple[str, list, str]] = []
    for r in raw_rules:
        if r.get("effect") != "define":
            continue
        if r.get("resource_type", "FilePath") not in ("FilePath", "*"):
            continue
        define_block = r.get("define") or {}
        # is_safe_variant must come first (first-match-wins in entity builder).
        for attr, action in (("is_safe_variant", "allow"), ("is_secret", "block")):
            entry = define_block.get(attr)
            if not isinstance(entry, dict):
                continue
            patterns = entry.get("patterns") or []
            if not patterns:
                continue
            compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
            result.append((action, compiled, ""))
    return result


def load_allowed_domains() -> list[str]:
    """Return the list of network-allowed domains from the manifest.

    Reads policy.network.allowed_domains.  This is the same list that
    BuildManagedSettings() uses for WebFetch allowRules in managed-settings.json,
    including any ecosystem domains merged in by MergeRepoManifest.

    Used by restrict_network.py to populate NetworkResource.in_allowlist.
    """
    policy = _load_policy()
    return list(policy.get("network", {}).get("allowed_domains", []))


def load_rules() -> list[Rule]:
    """Return Rule objects for engine evaluation.

    If policy.rules is present in the manifest, parse them directly
    (Phase 2+ native format).  Otherwise, run the compat adapter to
    translate the legacy bash / secrets / network sections into v0: Rules
    and emit a deprecation warning.
    """
    policy = _load_policy()

    if "rules" in policy:
        return _parse_direct_rules(policy["rules"])

    print(
        "[contained] DEPRECATION: policy.secrets/bash/network sections are deprecated. "
        "Run `contained migrate` to upgrade to the unified policy.rules format.",
        file=sys.stderr,
    )
    return _compat_adapter(policy)


def load_rules_from_path(manifest_path: str) -> list[Rule]:
    """Load Rule objects from an arbitrary manifest file path.

    Unlike load_rules(), this function is not cached and reads from the
    given path directly. Used by the validator CLI at build time.
    """
    with Path(manifest_path).open() as fh:
        manifest = yaml.safe_load(fh) or {}
    policy = _deep_merge(_DEFAULTS, manifest.get("policy", {}))
    if "rules" in policy:
        return _parse_direct_rules(policy["rules"])
    return _compat_adapter(policy)


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_policy() -> dict[str, Any]:
    """Load, merge with defaults, and cache the policy section of the manifest.

    Tries the baked-in image path first, then the workspace path.
    Always succeeds: on any read/parse error the structural defaults are
    returned so that hooks receive empty rule lists (no checks applied).
    """
    for path in (_MANIFEST_PATH, _WORKSPACE_MANIFEST_PATH):
        try:
            with path.open() as fh:
                manifest = yaml.safe_load(fh) or {}
            return _deep_merge(_DEFAULTS, manifest.get("policy", {}))
        except Exception:
            continue
    return dict(_DEFAULTS)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    result = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Phase 2+: direct rules parsing
# ---------------------------------------------------------------------------


def _parse_direct_rules(raw_rules: list[dict]) -> list[Rule]:
    """Parse a direct policy.rules list into Rule objects."""
    rules: list[Rule] = []
    for r in raw_rules:
        effect = r["effect"]
        action = r.get("action") or []
        if isinstance(action, str):
            action = [action]
        rules.append(
            Rule(
                id=r["id"],
                effect=effect,
                action=action,
                resource_type=r.get("resource_type", "*"),
                when=r.get("when", []),
                unless=r.get("unless", []),
                reason=r.get("reason"),
                tags=r.get("tags", []),
                define=r.get("define") if effect == "define" else None,
            )
        )
    return rules


# ---------------------------------------------------------------------------
# Phase 1 compat adapter
# ---------------------------------------------------------------------------


def _compat_adapter(policy: dict) -> list[Rule]:
    """Translate legacy manifest sections into v0: Rule objects.

    Secrets rules
    -------------
    block → forbid on _FILE_ACTIONS / FilePath when is_secret == true,
            unless is_safe_variant == true (the unless makes Cedar evaluation
            equivalent to the original allow-first regex matching: a safe
            variant file satisfies the unless clause, preventing the forbid
            from matching).
    allow → (safe-variant rules) emitted as permit rules; the forbid's unless
            clause is the primary guard, but the permit makes the outcome
            explicit ALLOW rather than DEFER for safe variants.

    Bash rules
    ----------
    Each pattern becomes one Rule (one Rule per pattern, not per rule name),
    using the matches_re operator on resource.raw.  This mirrors the original
    regex-on-full-command-string semantics.
    allow → permit; block → forbid.

    Network
    -------
    If network.enabled is true, a single forbid rule blocks requests to
    domains outside the allowlist (resource.in_allowlist == false).
    """
    rules: list[Rule] = []

    # --- Secrets ---
    for rule_def in policy.get("secrets", {}).get("rules", []):
        name = rule_def.get("name", "unnamed")
        action = rule_def.get("action", "block")
        reason = rule_def.get("reason")
        rule_id = f"v0:secrets:{name}"

        if action == "allow":
            # Safe-variant permit: explicit ALLOW so the outcome is not DEFER.
            rules.append(
                Rule(
                    id=rule_id,
                    effect="permit",
                    action=_FILE_ACTIONS,
                    resource_type="FilePath",
                    when=["resource.is_safe_variant == true"],
                    reason=reason,
                    tags=["v0", "secrets", "compat"],
                )
            )
        else:
            # Block secret files, but not safe variants (allow overrides block
            # for safe-variant paths because the unless clause is satisfied).
            rules.append(
                Rule(
                    id=rule_id,
                    effect="forbid",
                    action=_FILE_ACTIONS,
                    resource_type="FilePath",
                    when=["resource.is_secret == true"],
                    unless=["resource.is_safe_variant == true"],
                    reason=reason,
                    tags=["v0", "secrets", "compat"],
                )
            )

    # --- Bash ---
    for rule_def in policy.get("bash", {}).get("rules", []):
        name = rule_def.get("name", "unnamed")
        action = rule_def.get("action", "block")
        reason = rule_def.get("reason")
        effect = "permit" if action == "allow" else "forbid"

        for i, pattern in enumerate(rule_def.get("patterns", [])):
            rule_id = f"v0:bash:{name}:{i}"
            # Single-quote the pattern so _parse_rhs strips them cleanly.
            # Manifest patterns are loaded from YAML with literal backslashes
            # (YAML single-quoted scalars), which re.search interprets as
            # regex metacharacters (\s = whitespace, \b = word boundary).
            rules.append(
                Rule(
                    id=rule_id,
                    effect=effect,
                    action=["Bash"],
                    resource_type="BashCommand",
                    when=[f"resource.raw matches_re '{pattern}'"],
                    reason=reason,
                    tags=["v0", "bash", "compat"],
                )
            )

    # --- Network ---
    net = policy.get("network", {})
    if net.get("enabled", False):
        rules.append(
            Rule(
                id="v0:network:allowlist",
                effect="forbid",
                action=_NETWORK_ACTIONS,
                resource_type="NetworkResource",
                when=["resource.in_allowlist == false"],
                reason="Network request to a domain outside the operator allowlist.",
                tags=["v0", "network", "compat"],
            )
        )

    return rules
