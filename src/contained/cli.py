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
        import os

        from contained.session import _print_splash, start_repl

        if os.environ.get("contAIned_FORCE_LOCAL") != "1":
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
    "--force",
    "-f",
    is_flag=True,
    default=False,
    help="Re-run the setup wizard even if already initialised.",
)
@click.option(
    "--rebuild",
    "-r",
    is_flag=True,
    default=False,
    help="Force a full Docker image rebuild even if the image is already up to date.",
)
@click.option(
    "--manifest",
    "manifest_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True, resolve_path=True),
    help=(
        "Path to a manifest.yaml to bake into the Docker image. "
        "Skips the interactive wizard. Suitable for CI/CD pipelines."
    ),
)
def init(directory: str, force: bool, rebuild: bool, manifest_path: str | None) -> None:
    """
    Initialise a contAIned workspace.

    Scaffolds .contAIned/, .claude/, and CLAUDE.md in DIRECTORY (default: current
    directory).  Re-running refreshes hook files and syncs the manifest.

    The manifest is baked into the Docker image so policy is enforced at the
    highest-precedence settings level and cannot be overridden at runtime.

    \b
    Examples:
      contAIned init                         # initialise with interactive wizard
      contAIned init ./myrepo                # initialise in a specific directory
      contAIned init --manifest policy.yaml  # non-interactive, bake provided manifest
      contAIned init --force                 # re-run setup wizard (reconfigure)
      contAIned init --rebuild               # force-rebuild the Docker image
    """
    from contained.init import run_init
    from contained.session import _print_splash

    _print_splash()
    run_init(
        Path(directory),
        force=force,
        rebuild=rebuild,
        manifest_path=Path(manifest_path) if manifest_path else None,
    )
