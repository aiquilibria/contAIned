"""Interactive REPL — spawns the native claude CLI in the current terminal.

``contAIned repl`` runs ``claude`` as a direct child process with the operator's
terminal inherited.  All I/O passes through unmodified — there is no PTY proxy
or input interception layer.

Hash commands (``#db``, ``#status``, ``#update``, ``#review``) are
handled entirely by the ``UserPromptSubmit`` hook that Claude Code invokes
before each prompt is processed.

Docker mode
-----------
``docker_runner.py`` runs ``docker run -it ... contained:latest repl`` on the host.
``contAIned_FORCE_LOCAL=1`` causes the in-container ``contAIned repl`` to skip docker
delegation and run ``claude`` directly in the current terminal.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rich.console import Console

from .runner import (
    _check_initialised,
    _get_tracer,
    _load_manifest,
    _load_model_config,
    _print_runtime_banner,
)

console = Console()


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
        runtime  = manifest.get("runtime", {})
        from contained.docker_runner import DockerRunner
        _print_runtime_banner(root)
        DockerRunner(runtime.get("docker", {}), root).run_repl()
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

    cmd = ["claude"]
    model = _load_model_config(root)
    if model:
        cmd += ["--model", model]

    console.print("[dim]Claude Code is starting up — input may appear delayed.[/dim]")

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
