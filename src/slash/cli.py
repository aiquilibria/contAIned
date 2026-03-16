"""
slash — a slash coding agent CLI.

Commands:
  slash init          Initialise a workspace in the current directory
  slash update        Refresh managed hook files from latest templates
  slash run <task>    Run the agent on a task
  slash repl          Start an interactive REPL session
  slash status        Show a summary of recent audit events
  slash review        Review tasks awaiting operator sign-off
  slash gc            Prune old task data from tracer.db
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


@click.group(invoke_without_command=True)
@click.version_option("0.1.0", prog_name="slash")
@click.pass_context
def main(ctx: click.Context) -> None:
    """slash — a slash coding agent CLI."""
    if ctx.invoked_subcommand is None:
        # No subcommand given — drop straight into the REPL.
        from slash.repl import start_repl
        start_repl(_find_root(), verbosity=None)


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

    Scaffolds .slash/, .claude/, and workspace/ in DIRECTORY (default: current directory).
    Strictly additive — never overwrites existing files.
    Detects git and updates .gitignore automatically.

    To refresh hook files after upgrading slash, use: slash update
    To re-run the setup wizard (e.g. to switch to Docker), use: slash init --force
    To force a Docker image rebuild without re-running the wizard, use: slash init --rebuild

    \b
    Examples:
      slash init            # initialise in current directory
      slash init ./myrepo   # initialise in a specific directory
      slash init --force    # re-run setup wizard (reconfigure runtime, docker, etc.)
      slash init --rebuild  # force-rebuild the Docker image (docker mode only)
    """
    from slash.init import run_init
    run_init(Path(directory), force=force, rebuild=rebuild)


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
      .slash/manifest.yaml

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
    in .slash/manifest.yaml.  In Docker mode, filesystem isolation is also
    enforced by the container runtime (kernel namespaces + bind mounts).

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
    Print a summary of recent audit events for *root*.

    Primary source: ``audit_events`` table in ``.slash/tracer.db``.
    Fallback:       ``.slash/audit/pipeline.jsonl`` (legacy, or when
                    ``policy.audit.jsonl_export`` is enabled).

    Extracted as a standalone callable so it can be reused by the REPL's
    ``/status`` built-in without going through Click.
    """
    db_path  = root / ".slash" / "tracer.db"
    log_path = root / ".slash" / "audit" / "pipeline.jsonl"

    entries: list[dict] = []
    source_label: str = ""

    # ── Primary: tracer.db ────────────────────────────────────────────────────
    if db_path.exists():
        try:
            from slash.tracer import SlashTracer  # noqa: PLC0415
            tracer  = SlashTracer(str(db_path))
            rows    = tracer.recent_audit_events(limit=tail)
            entries = rows  # already newest-first, already dicts
            source_label = str(db_path.relative_to(root))
        except Exception:
            pass

    # ── Fallback: pipeline.jsonl ──────────────────────────────────────────────
    if not entries and log_path.exists():
        lines = log_path.read_text().strip().splitlines()
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        entries = list(reversed(entries[-tail:]))  # newest first
        source_label = str(log_path.relative_to(root))

    if not entries:
        console.print("\n[dim]No audit events found. Run a task first.[/dim]\n")
        return

    console.print(
        f"\n[bold]Audit log[/bold] — last {len(entries)} entries (newest first)\n"
    )

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Timestamp", style="dim", no_wrap=True)
    table.add_column("Session", style="dim", no_wrap=True)
    table.add_column("Tool")
    table.add_column("Input summary", style="dim")
    table.add_column("Outcome")

    for entry in entries:
        ts      = (entry.get("ts") or "")[:19].replace("T", " ")
        sid     = (entry.get("session_id") or "")[:8]
        tool    = entry.get("tool") or ""
        outcome = entry.get("outcome") or ""

        inp = entry.get("input") or {}
        if isinstance(inp, dict):
            input_summary = (
                inp.get("command")
                or inp.get("file_path")
                or inp.get("file_paths") and ", ".join(inp["file_paths"])
                or inp.get("pattern")
                or inp.get("prompt_head")
                or str(inp)[:60]
            ) or ""
        else:
            input_summary = str(inp)[:60]

        if len(input_summary) > 60:
            input_summary = input_summary[:57] + "…"

        outcome_styled = (
            "[green]success[/green]" if outcome == "success"
            else f"[red]{outcome}[/red]"
        )

        table.add_row(ts, sid, tool, input_summary, outcome_styled)

    console.print(table)
    if source_label:
        console.print(f"\n[dim]Source: {source_label}[/dim]\n")
    else:
        console.print()


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


# ── review ────────────────────────────────────────────────────────────────────

@main.command()
@click.option(
    "--dir", "-d",
    default=None,
    type=click.Path(file_okay=False, exists=True, resolve_path=True),
    help="Workspace root (default: auto-detected from cwd)",
)
def review(dir: str | None) -> None:
    """
    Review tasks awaiting operator sign-off.

    Lists all pending_review tasks and lets you approve or dismiss each one.
    Approve → marks the task closed.
    Dismiss → marks the task abandoned.

    \b
    Examples:
      slash review
    """
    root = Path(dir) if dir else _find_root()
    db_path = root / ".slash" / "tracer.db"

    if not db_path.exists():
        console.print("\n[dim]No tracer database found. Run a task first.[/dim]\n")
        return

    try:
        from slash.tracer import SlashTracer  # noqa: PLC0415
        tracer = SlashTracer(str(db_path))
    except Exception as exc:
        console.print(f"\n[red]Could not open tracer database: {exc}[/red]\n")
        return

    reviews = tracer.get_pending_reviews()
    if not reviews:
        console.print("\n[dim]No tasks awaiting review.[/dim]\n")
        return

    console.print(f"\n[bold]Pending reviews ({len(reviews)}):[/bold]\n")
    for i, r in enumerate(reviews, 1):
        from datetime import datetime, timezone  # noqa: PLC0415
        dt = datetime.fromtimestamp(r["started_at"] / 1000, tz=timezone.utc)
        console.print(
            f"  [bold]{i}.[/bold] [dim]{r['session_id'][:12]}…[/dim]"
            f"  \"{r['prompt'][:60]}\"  [dim]({dt.strftime('%Y-%m-%d %H:%M')})[/dim]"
        )
    console.print()

    # Let the operator pick one to review
    try:
        choice = input(f"Pick a task [1–{len(reviews)}/all/cancel] › ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    targets: list[dict] = []
    if choice == "all":
        targets = reviews
    elif choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(reviews):
            targets = [reviews[idx]]
        else:
            console.print("[red]Invalid selection.[/red]")
            return
    else:
        console.print("[dim]Cancelled.[/dim]\n")
        return

    for r in targets:
        _print_review(tracer, r["session_id"], r["prompt"])


def _print_review(tracer: object, session_id: str, prompt: str) -> None:
    """
    Display one pending review and prompt the operator to approve or dismiss.
    Intended for the standalone ``slash review`` CLI flow.
    """
    from rich.panel import Panel  # noqa: PLC0415
    from rich.text  import Text   # noqa: PLC0415

    # Load stored summary
    try:
        row = tracer.conn.execute(  # type: ignore[attr-defined]
            "SELECT prompt, summary, started_at FROM tasks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        task_prompt  = row[0] if row else prompt
        stored_sum   = json.loads(row[1]) if row and row[1] else {}
        task_started = row[2] if row else None
    except Exception:
        task_prompt  = prompt
        stored_sum   = {}
        task_started = None

    file_changes = stored_sum.get("file_changes", [])
    action_log   = stored_sum.get("action_log", [])

    # Header
    header = Text()
    header.append("Task Review", style="bold white")
    header.append(f"\n{task_prompt[:120]}", style="dim")
    if task_started:
        from datetime import datetime, timezone  # noqa: PLC0415
        dt = datetime.fromtimestamp(task_started / 1000, tz=timezone.utc)
        header.append(f"\n  started {dt.strftime('%Y-%m-%d %H:%M UTC')}", style="dim")
    console.print(Panel(header, border_style="blue", expand=False))

    # File changes
    if file_changes:
        console.print(f"\n[bold] File Changes[/bold]  [dim]({len(file_changes)} file(s))[/dim]\n")
        for fc in file_changes:
            fp  = fc.get("file_path", "?")
            add = fc.get("lines_added", 0)
            rem = fc.get("lines_removed", 0)
            try:
                diff_text  = tracer.diff_task(session_id, fp)  # type: ignore[attr-defined]
                diff_lines = diff_text.splitlines() if diff_text else []
            except Exception:
                diff_lines = []
            console.print(
                f"  [bold cyan]{fp}[/bold cyan]"
                f"  [green]+{add}[/green]  [red]-{rem}[/red]"
            )
            if diff_lines:
                diff_out = Text()
                for ln in diff_lines[:200]:
                    if ln.startswith("+++") or ln.startswith("---"):
                        diff_out.append(ln + "\n", style="dim")
                    elif ln.startswith("@@"):
                        diff_out.append(ln + "\n", style="cyan")
                    elif ln.startswith("+"):
                        diff_out.append(ln + "\n", style="green")
                    elif ln.startswith("-"):
                        diff_out.append(ln + "\n", style="red")
                    else:
                        diff_out.append(ln + "\n")
                if len(diff_lines) > 200:
                    diff_out.append("  … (diff truncated)\n", style="dim")
                console.print(diff_out)
    else:
        console.print("\n[dim]  No file changes recorded.[/dim]\n")

    # Action log (notable entries only)
    notable = [
        e for e in action_log
        if e.get("tool") in ("Bash", "Agent") or e.get("outcome") == "denied"
    ]
    if notable:
        console.print(f"[bold] Action Log[/bold]  [dim]({len(notable)} entries)[/dim]\n")
        for e in notable[-20:]:
            inp = e.get("input") or {}
            if e.get("tool") == "Bash":
                cmd  = (inp.get("command") or "")[:80]
                ec   = inp.get("exit_code")
                ec_s = f" (exit: {ec})" if ec is not None else ""
                console.print(f"  ● bash: {cmd}{ec_s}")
            elif e.get("tool") == "Agent":
                atype = inp.get("agent_type") or "agent"
                ph    = (inp.get("prompt_head") or "")[:60]
                console.print(f"  ● agent [{atype}]: {ph}")
            elif e.get("outcome") == "denied":
                console.print(
                    f"  [red]✗ {e.get('tool')} denied: {(e.get('reason') or '')[:80]}[/red]"
                )
        console.print()

    # Decision
    console.print("[bold]Decision:[/bold]  [a] Approve   [d] Dismiss\n", end="")
    try:
        choice = input("  › ").strip().lower()[:1]
    except (EOFError, KeyboardInterrupt):
        choice = ""

    if choice == "d":
        try:
            tracer.set_task_status(session_id, "abandoned")  # type: ignore[attr-defined]
            console.print("[yellow]Task dismissed.[/yellow]\n")
        except Exception:
            pass
    else:
        try:
            tracer.set_task_status(session_id, "closed")  # type: ignore[attr-defined]
            console.print("[green]Task approved.[/green]\n")
        except Exception:
            pass


# ── gc ─────────────────────────────────────────────────────────────────────────

@main.command()
@click.option(
    "--dir", "-d",
    default=None,
    type=click.Path(file_okay=False, exists=True, resolve_path=True),
    help="Workspace root (default: auto-detected from cwd)",
)
@click.option(
    "--keep-days",
    default=14,
    show_default=True,
    help="Retain data for this many days; older closed/abandoned data is pruned.",
)
def gc(dir: str | None, keep_days: int) -> None:
    """
    Prune old task data from tracer.db.

    Removes snapshots, baselines, orphaned blobs, and old audit events for
    tasks that are closed or abandoned and older than --keep-days.

    Tasks in open or pending_review state are never pruned.

    \b
    Examples:
      slash gc                   # prune data older than 14 days (default)
      slash gc --keep-days 30    # keep data for 30 days
      slash gc --keep-days 0     # prune everything not currently active
    """
    root    = Path(dir) if dir else _find_root()
    db_path = root / ".slash" / "tracer.db"

    if not db_path.exists():
        console.print("\n[dim]No tracer database found — nothing to prune.[/dim]\n")
        return

    try:
        from slash.tracer import SlashTracer  # noqa: PLC0415
        tracer = SlashTracer(str(db_path))
    except Exception as exc:
        console.print(f"\n[red]Could not open tracer database: {exc}[/red]\n")
        return

    # Snapshot row counts before GC for the report
    def _count(table: str) -> int:
        try:
            return tracer.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            return -1

    before = {t: _count(t) for t in ("blobs", "snapshots", "baselines", "audit_events", "tasks")}

    tracer.gc(keep_days=keep_days)

    after = {t: _count(t) for t in before}

    console.print(f"\n[bold]slash gc[/bold] — pruned data older than {keep_days} day(s)\n")
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Table")
    table.add_column("Before", justify="right", style="dim")
    table.add_column("After",  justify="right")
    table.add_column("Removed", justify="right", style="dim")

    for tname in ("tasks", "snapshots", "baselines", "blobs", "audit_events"):
        b = before[tname]
        a = after[tname]
        removed = (b - a) if b >= 0 and a >= 0 else "?"
        removed_str = f"[red]-{removed}[/red]" if removed else "[dim]0[/dim]"
        table.add_row(tname, str(b), str(a), removed_str)

    console.print(table)
    console.print(f"\n[dim]Database: {db_path}[/dim]\n")


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
