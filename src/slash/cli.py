"""
slash — a slash coding agent CLI.

Commands:
  slash init          Initialise a workspace in the current directory
  slash update        Refresh managed hook files from latest templates
  slash run <task>    Run the agent on a task
  slash status        Show a summary of the audit log
"""
import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()


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


@click.group()
@click.version_option("0.1.0", prog_name="slash")
def main() -> None:
    """slash — a slash coding agent CLI."""
    pass


# ── init ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument(
    "directory",
    default=".",
    type=click.Path(file_okay=False, writable=True, resolve_path=True),
)
def init(directory: str) -> None:
    """
    Initialise a slash workspace.

    Scaffolds .slash/, .claude/, and workspace/ in DIRECTORY (default: current directory).
    Strictly additive — never overwrites existing files.
    Detects git and updates .gitignore automatically.

    To refresh hook files after upgrading slash, use: slash update

    \b
    Examples:
      slash init            # initialise in current directory
      slash init ./myrepo   # initialise in a specific directory
    """
    from slash.init import run_init
    run_init(Path(directory))


# ── update ────────────────────────────────────────────────────────────────────

@main.command()
@click.option(
    "--dir", "-d",
    default=None,
    type=click.Path(file_okay=False, exists=True, resolve_path=True),
    help="Workspace root (default: auto-detected from cwd)",
)
def update(dir: str | None) -> None:
    """
    Refresh managed hook files from the latest templates.

    Overwrites .slash/hooks/ and .claude/settings.json with the versions
    bundled in the currently installed slash package.

    Safe to run after upgrading slash. Never touches user-editable files:
      .slash/policy/manifest.yaml

    \b
    Examples:
      slash update
    """
    from slash.init import run_update
    root = Path(dir) if dir else _find_root()
    run_update(root)


# ── run ───────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("task")
@click.option(
    "--dir", "-d",
    default=None,
    type=click.Path(file_okay=False, exists=True, resolve_path=True),
    help="Workspace root (default: auto-detected from cwd)",
)
def run(task: str, dir: str | None) -> None:
    """
    Run the agent on a TASK.

    All tool calls are governed by the hooks in .slash/hooks/ and the policy
    in .slash/policy/manifest.yaml.

    \b
    Examples:
      slash run "Add docstrings to all functions in utils.py"
      slash run "Write unit tests for the auth module"
    """
    from slash.runner import run_task
    root = Path(dir) if dir else _find_root()
    run_task(task, root)


# ── status ────────────────────────────────────────────────────────────────────

def _print_status(root: Path, tail: int = 20) -> None:
    """
    Print a summary of the audit log for *root*.

    Extracted as a standalone callable so it can be reused by the REPL's
    ``/status`` built-in without going through Click.
    """
    log_path = root / ".slash" / "audit" / "pipeline.jsonl"

    if not log_path.exists():
        console.print("\n[dim]No audit log found. Run a task first.[/dim]\n")
        return

    lines = log_path.read_text().strip().splitlines()
    if not lines:
        console.print("\n[dim]Audit log is empty.[/dim]\n")
        return

    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    recent = list(reversed(entries[-tail:]))

    console.print(f"\n[bold]Audit log[/bold] — last {len(recent)} of {len(entries)} entries (newest first)\n")

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Timestamp", style="dim", no_wrap=True)
    table.add_column("Tool")
    table.add_column("Input summary", style="dim")
    table.add_column("Outcome")

    for entry in recent:
        ts = entry.get("ts", "")[:19].replace("T", " ")
        tool = entry.get("tool", "")
        outcome = entry.get("outcome", "")

        inp = entry.get("input") or {}
        if isinstance(inp, dict):
            summary = (
                inp.get("command")
                or inp.get("file_path")
                or inp.get("pattern")
                or str(inp)[:60]
            )
        else:
            summary = str(inp)[:60]

        outcome_styled = (
            "[green]success[/green]" if outcome == "success"
            else f"[red]{outcome}[/red]"
        )

        table.add_row(ts, tool, summary, outcome_styled)

    console.print(table)
    console.print(f"\n[dim]Full log: {log_path}[/dim]\n")


@main.command()
@click.option(
    "--dir", "-d",
    default=None,
    type=click.Path(file_okay=False, exists=True, resolve_path=True),
    help="Workspace root (default: auto-detected from cwd)",
)
@click.option("--tail", "-n", default=20, help="Number of most recent entries to show")
def status(dir: str | None, tail: int) -> None:
    """
    Show a summary of the audit log.

    \b
    Examples:
      slash status
      slash status --tail 50
    """
    root = Path(dir) if dir else _find_root()
    _print_status(root, tail)


# ── repl ──────────────────────────────────────────────────────────────────────

@main.command()
@click.option(
    "--dir", "-d",
    default=None,
    type=click.Path(file_okay=False, exists=True, resolve_path=True),
    help="Workspace root (default: auto-detected from cwd)",
)
@click.option(
    "--verbosity",
    type=click.Choice(["verbose", "concise", "none"]),
    default=None,
    help="Override manifest verbosity for this session.",
)
def repl(dir: str | None, verbosity: str | None) -> None:
    """
    Start an interactive REPL session (persistent agent conversation).

    Every message you type is forwarded to the same living agent session.
    Conversation history accumulates turn-by-turn until you type /new or exit.

    \b
    Built-in commands (handled locally, never sent to the agent):
      /new      Start a fresh session
      /status   Show recent audit-log entries
      /help     Show the full command list
      /clear    Clear the terminal
      /exit     Quit  (alias: /quit, Ctrl-D)

    \b
    Examples:
      slash repl
      slash repl --verbosity concise
    """
    from slash.repl import start_repl
    root = Path(dir) if dir else _find_root()
    start_repl(root, verbosity)
