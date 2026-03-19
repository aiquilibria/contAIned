"""
slash — a slash coding agent CLI.

Commands:
  slash        Start an interactive REPL session (default when no subcommand given)
  slash init   Initialise a workspace in the current directory
"""
from pathlib import Path

import click


def _find_root() -> Path:
    """
    Return the slash workspace root.
    Walks up from cwd looking for a .slash/ directory.
    Falls back to cwd if not found (init will create it there).
    """
    current = Path.cwd().resolve()
    while current != current.parent:
        if (current / ".slash").is_dir():
            return current
        current = current.parent
    return Path.cwd().resolve()


@click.group(invoke_without_command=True)
@click.version_option("0.1.0", prog_name="slash")
@click.pass_context
def main(ctx: click.Context) -> None:
    """slash — a slash coding agent CLI."""
    if ctx.invoked_subcommand is None:
        from slash.repl import start_repl
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
    Initialise a slash workspace.

    Scaffolds .slash/, .claude/, and CLAUDE.md in DIRECTORY (default: current
    directory).  Re-running refreshes hook files and syncs the manifest.

    \b
    Examples:
      slash init            # initialise in current directory
      slash init ./myrepo   # initialise in a specific directory
      slash init --force    # re-run setup wizard (reconfigure model, docker, etc.)
      slash init --rebuild  # force-rebuild the Docker image without re-running wizard
    """
    from slash.init import run_init
    run_init(Path(directory), force=force, rebuild=rebuild)
