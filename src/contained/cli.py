"""
contAIned — a contAIned coding agent CLI.

Commands:
  contAIned        Start an interactive REPL session (default when no subcommand given)
  contAIned init   Initialise a workspace in the current directory
"""
from pathlib import Path

import click


def _find_root() -> Path:
    """
    Return the contAIned workspace root.
    Walks up from cwd looking for a .contAIned/ directory.
    Falls back to cwd if not found (init will create it there).
    """
    current = Path.cwd().resolve()
    while current != current.parent:
        if (current / ".contAIned").is_dir():
            return current
        current = current.parent
    return Path.cwd().resolve()


@click.group(invoke_without_command=True)
@click.version_option("0.1.0", prog_name="contAIned")
@click.pass_context
def main(ctx: click.Context) -> None:
    """contAIned — a contAIned coding agent CLI."""
    if ctx.invoked_subcommand is None:
        from contained.runner import _print_splash
        from contained.repl import start_repl
        _print_splash()
        start_repl(_find_root())


# ── init ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument(
    "directory",
    default=".",
    type=click.Path(file_okay=False, writable=True, resolve_path=True),
)
@click.option(
    "--force", "-f",
    is_flag=True,
    default=False,
    help="Re-run the setup wizard even if already initialised.",
)
@click.option(
    "--rebuild", "-r",
    is_flag=True,
    default=False,
    help="Force a full Docker image rebuild even if the image is already up to date.",
)
def init(directory: str, force: bool, rebuild: bool) -> None:
    """
    Initialise a contAIned workspace.

    Scaffolds .contAIned/, .claude/, and CLAUDE.md in DIRECTORY (default: current
    directory).  Re-running refreshes hook files and syncs the manifest.

    \b
    Examples:
      contAIned init            # initialise in current directory
      contAIned init ./myrepo   # initialise in a specific directory
      contAIned init --force    # re-run setup wizard (reconfigure model, docker, etc.)
      contAIned init --rebuild  # force-rebuild the Docker image without re-running wizard
    """
    from contained.runner import _print_splash
    from contained.init import run_init
    _print_splash()
    run_init(Path(directory), force=force, rebuild=rebuild)
