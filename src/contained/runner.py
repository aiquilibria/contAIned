"""
contAIned runner — workspace utilities shared by the REPL and CLI.

The SDK-dependent code (``run_task``, ``_build_client``, ``_render_message``,
``_run_task_streaming``, ``_extract_narrative``, ``_print_result_summary``,
``_load_thinking_config``) has been removed.  ``contAIned run`` is deprecated;
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

    Checks ``.contAIned/manifest.yaml`` first (new location), then falls back to
    ``.contAIned/policy/manifest.yaml`` (legacy location) for backwards
    compatibility with workspaces initialised before the path migration.
    """
    new_path = root / ".contAIned" / "manifest.yaml"
    old_path = root / ".contAIned" / "policy" / "manifest.yaml"
    manifest_path = new_path if new_path.exists() else old_path
    try:
        return yaml.safe_load(manifest_path.read_text()) or {}
    except FileNotFoundError:
        return {}


def _load_model_config(root: Path) -> str | None:
    """
    Read ``agent.model`` from ``.contAIned/manifest.yaml``.

    Returns the model string (e.g. ``"claude-sonnet-4-6"``), or ``None`` if
    not set — in which case the ``claude`` CLI uses its own default.
    """
    return _load_manifest(root).get("agent", {}).get("model") or None


def _check_initialised(root: Path) -> list[str]:
    """Return a list of missing paths that indicate init has not been run."""
    required = [
        root / ".contAIned" / "hooks" / "restrict_writes.py",
        root / ".contAIned" / "hooks" / "audit.py",
        root / ".contAIned" / "hooks" / "qa.py",
        root / ".claude" / "settings.json",
    ]
    missing = [str(p.relative_to(root)) for p in required if not p.exists()]

    # Accept either new or legacy manifest path
    manifest_new = root / ".contAIned" / "manifest.yaml"
    manifest_old = root / ".contAIned" / "policy" / "manifest.yaml"
    if not manifest_new.exists() and not manifest_old.exists():
        missing.append(".contAIned/manifest.yaml")

    return missing


def _get_tracer(root: Path):
    """Return a :class:`~contAIned.tracer.contAInedTracer` for *root*, or ``None`` if unavailable."""
    try:
        from contained.tracer import contAInedTracer  # noqa: PLC0415
        return contAInedTracer(str(root / ".contAIned" / "tracer.db"))
    except Exception:
        return None


def _print_runtime_banner(root: Path) -> None:
    """Print a short runtime info line when starting a session."""
    manifest = _load_manifest(root)
    image    = manifest.get("runtime", {}).get("docker", {}).get("image", "contained:latest")
    console.print(f"[dim][contAIned] runtime: docker ({image})[/dim]")
    console.print(f"[dim][contAIned] workspace: {root}[/dim]\n")
