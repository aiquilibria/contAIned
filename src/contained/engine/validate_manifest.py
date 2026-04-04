"""Manifest policy validator — blocking mode entry point.

Called at docker build time by the Dockerfile:
    python3 -m contained.engine.validate_manifest /etc/contained/manifest.yaml

Exits 0 on success (no errors). Exits 1 if validation errors are found.
Warnings are printed but do not cause a non-zero exit.
"""

from __future__ import annotations

import sys

from contained.engine.policy import load_rules_from_path
from contained.engine.validator import validate_rules


def main(manifest_path: str) -> int:
    try:
        rules = load_rules_from_path(manifest_path)
    except Exception as exc:
        print(
            f"[contained] policy validation: failed to load rules from {manifest_path}: {exc}",
            file=sys.stderr,
        )
        return 1

    result = validate_rules(rules, phase=2)

    if result.issues:
        print("[contained] policy validation:")
        result.print_report(phase=2)

    if result.errors:
        print(
            f"\n[contained] policy validation FAILED: "
            f"{len(result.errors)} error(s). Fix the manifest and rebuild.",
            file=sys.stderr,
        )
        return 1

    if result.warnings:
        print(f"[contained] policy validation passed with {len(result.warnings)} warning(s).")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python3 -m contained.engine.validate_manifest <manifest.yaml>",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
