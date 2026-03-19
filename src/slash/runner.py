"""
slash runner — workspace utilities shared by the REPL and CLI.

The SDK-dependent code (``run_task``, ``_build_client``, ``_render_message``,
``_run_task_streaming``, ``_extract_narrative``, ``_print_result_summary``,
``_load_thinking_config``) has been removed.  ``slash run`` is deprecated;
the interactive REPL is the primary interface.

This module now contains only the lightweight helpers that have no dependency
on ``claude-agent-sdk``.
"""
from __future__ import annotations

from typing import Any

import yaml
from pathlib import Path

from rich.console import Console

console = Console()


def _load_manifest(root: Path) -> dict[str, Any]:
    """
    Load and return the parsed manifest, or an empty dict if missing.

    Checks ``.slash/manifest.yaml`` first (new location), then falls back to
    ``.slash/policy/manifest.yaml`` (legacy location) for backwards
    compatibility with workspaces initialised before the path migration.
    """
    new_path = root / ".slash" / "manifest.yaml"
    old_path = root / ".slash" / "policy" / "manifest.yaml"
    manifest_path = new_path if new_path.exists() else old_path
    try:
        return yaml.safe_load(manifest_path.read_text()) or {}
    except FileNotFoundError:
        return {}


def _load_model_config(root: Path) -> str | None:
    """
    Read ``agent.model`` from ``.slash/manifest.yaml``.

    Returns the model string (e.g. ``"claude-sonnet-4-6"``), or ``None`` if
    not set — in which case the ``claude`` CLI uses its own default.
    """
    return _load_manifest(root).get("agent", {}).get("model") or None


def _check_initialised(root: Path) -> list[str]:
    """Return a list of missing paths that indicate init has not been run."""
    required = [
        root / ".slash" / "hooks" / "restrict_writes.py",
        root / ".slash" / "hooks" / "audit.py",
        root / ".slash" / "hooks" / "qa.py",
        root / ".claude" / "settings.json",
    ]
    missing = [str(p.relative_to(root)) for p in required if not p.exists()]

    # Accept either new or legacy manifest path
    manifest_new = root / ".slash" / "manifest.yaml"
    manifest_old = root / ".slash" / "policy" / "manifest.yaml"
    if not manifest_new.exists() and not manifest_old.exists():
        missing.append(".slash/manifest.yaml")

    return missing


def _get_tracer(root: Path):
    """Return a :class:`~slash.tracer.SlashTracer` for *root*, or ``None`` if unavailable."""
    try:
        from slash.tracer import SlashTracer  # noqa: PLC0415
        return SlashTracer(str(root / ".slash" / "tracer.db"))
    except Exception:
        return None


def _print_runtime_banner(root: Path) -> None:
    """Print a short runtime info line when starting a session."""
    manifest = _load_manifest(root)
    image    = manifest.get("runtime", {}).get("docker", {}).get("image", "slash:latest")
    console.print(f"[dim][slash] runtime: docker ({image})[/dim]")
    console.print(f"[dim][slash] workspace: {root}[/dim]\n")
