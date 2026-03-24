"""
contAIned session — workspace utilities and REPL entry point.

This module owns:
- Manifest loading and model-config extraction
- Workspace initialisation checks
- Splash / runtime-banner display helpers
- ``start_repl``: the top-level entry point that either delegates to Docker
  (host side) or spawns ``claude`` directly (inside the container).

Docker mode
-----------
``docker_runner.py`` runs ``docker run -it ... contained:latest`` on the host.
``contAIned_FORCE_LOCAL=1`` causes the in-container ``contAIned`` process to
skip docker delegation and run ``claude`` directly in the current terminal.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

console = Console()


# ── workspace utilities ────────────────────────────────────────────────────────


def _print_splash() -> None:
    """Print the contAIned logo and tagline."""
    console.print(
        "\n[bold green]cont\\[[/bold green][bold red]AI[/bold red]"
        "[bold green]]ned[/bold green]"
        "  [dim]take back control of your agent![/dim]\n"
    )


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
    ]
    missing = [str(p.relative_to(root)) for p in required if not p.exists()]

    # Accept either new or legacy manifest path
    manifest_new = root / ".contAIned" / "manifest.yaml"
    manifest_old = root / ".contAIned" / "policy" / "manifest.yaml"
    if not manifest_new.exists() and not manifest_old.exists():
        missing.append(".contAIned/manifest.yaml")

    return missing


def _get_tracer(root: Path):
    """Return a :class:`~contAIned.tracer.contAInedTracer` for *root*.

    Returns ``None`` if unavailable.
    """
    try:
        from contained.tracer import contAInedTracer  # noqa: PLC0415

        return contAInedTracer(str(root / ".contAIned" / "tracer.db"))
    except Exception:
        return None


def _print_runtime_banner(root: Path) -> None:
    """Print a short runtime info line when starting a session."""
    manifest = _load_manifest(root)
    image = manifest.get("runtime", {}).get("docker", {}).get("image", "contained:latest")
    console.print(f"[dim][contAIned] runtime: docker ({image})[/dim]")
    console.print(f"[dim][contAIned] workspace: {root}[/dim]\n")


# ── REPL entry point ───────────────────────────────────────────────────────────


def start_repl(root: Path) -> None:
    """
    Entry point called from the CLI.

    **Docker mode:** delegates to ``DockerRunner.run_repl()`` unchanged.
    Inside the container, ``contAIned_FORCE_LOCAL=1`` causes this function to
    run ``claude`` directly in the current terminal.

    **Local mode:** validates the workspace, shows a pending-review banner if
    needed, then execs the native ``claude`` process.
    """
    force_local = os.environ.get("contAIned_FORCE_LOCAL") == "1"

    if not force_local:
        manifest = _load_manifest(root)
        runtime = manifest.get("runtime", {})

        # Pre-session provenance check when Sigstore is enabled.
        _sigstore_cfg = manifest.get("policy", {}).get("sigstore") or manifest.get("sigstore", {})
        if _sigstore_cfg.get("enabled", False):
            from contained.verify import _verify_workspace

            try:
                _verify_workspace(root)
                console.print("[dim][contAIned] sigstore: provenance verified ✓[/dim]")
            except RuntimeError as exc:
                console.print(
                    f"[red][contAIned] Sigstore verification failed — session blocked.[/red]\n"
                    f"  {exc}\n"
                    f"  Run [bold]contAIned init[/bold] to rebuild and re-sign."
                )
                raise SystemExit(1)

        from contained.docker_runner import DockerRunner

        _print_runtime_banner(root)
        DockerRunner(runtime.get("docker", {}), root, policy=manifest.get("policy", {})).run_repl()
        return

    missing = _check_initialised(root)
    if missing:
        console.print(
            "\n[red]Error:[/red] workspace not initialised. "
            "Run [bold]contAIned init[/bold] first.\n"
        )
        for m in missing:
            console.print(f"  [dim]{m}[/dim]")
        console.print()
        raise SystemExit(1)

    plugin_dir = Path(__file__).parent / "runtime" / "plugin"
    cmd = ["claude", "--plugin-dir", str(plugin_dir)]
    model = _load_model_config(root)
    if model:
        cmd += ["--model", model]

    # ── Pre-register work unit ────────────────────────────────────────────────
    # Open or find the work unit for the current branch before Claude starts,
    # so the unit exists before any tool call can occur.  session_id is not yet
    # known here; register_session_in_work_unit and record_policy_snapshot are
    # called in the UserPromptSubmit hook once the session is established.
    _tracer = _get_tracer(root)
    if _tracer is not None:
        try:
            _git_url = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                cwd=str(root),
                timeout=5,
            ).stdout.strip()
            _git_branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(root),
                timeout=5,
            ).stdout.strip()
            _git_base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(root),
                timeout=5,
            ).stdout.strip()
            if _git_branch and _git_base:
                _tracer.open_or_find_work_unit(
                    repo_url=_git_url or "local",
                    branch=_git_branch,
                    base_commit=_git_base,
                    prompt="",
                )
        except Exception:
            pass

    console.print("[dim]Claude Code is starting up — input may appear delayed.[/dim]\n")

    try:
        result = subprocess.run(cmd, cwd=str(root))
    except FileNotFoundError:
        console.print(
            "\n[red]Error:[/red] [bold]claude[/bold] not found.\n"
            "Install the Claude Code CLI: "
            "[dim]curl -fsSL https://claude.ai/install.sh | bash[/dim]\n"
        )
        raise SystemExit(1)

    if result.returncode != 0:
        console.print("\n[yellow]claude exited unexpectedly.[/yellow]\n")
