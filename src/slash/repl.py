"""Interactive REPL — keeps one agent session alive across turns."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread
import click
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.text import Text

from .runner import (
    _build_client,
    _get_tracer,
    _load_manifest,
    _load_verbosity_config,
    _print_result_summary,
    _print_runtime_banner,
    _render_message,
)

console = Console()

BUILTIN_PREFIX = "/"

BUILTIN_HELP = """\
Built-in REPL commands (handled locally, not sent to the agent):
  /new      Start a fresh session (old history discarded)
  /status   Show recent audit-log entries
  /review   Review tasks awaiting operator sign-off
  /update   Refresh managed hook files from latest templates (same as `slash update`)
  /help     Show this message
  /clear    Clear the terminal
  /sh <cmd> Run a shell command directly  (e.g. /sh git status)
  /exit     Quit the REPL  (alias: /quit, Ctrl-D)
"""

BUILTIN_COMMANDS = {"/help", "/new", "/status", "/review", "/update", "/clear", "/exit", "/quit", "/sh"}

# ── Tracer helpers ─────────────────────────────────────────────────────────────

_STALE_TASK_THRESHOLD_SECS = 3600  # 1 hour


def _render_review_summary(root: Path, session_id: str, prompt: str) -> None:
    """
    Re-render a stored task review summary to the terminal and prompt the
    operator for approve / dismiss.  Updates tracer.db accordingly.

    Used by both the startup pending-review check and the ``/review`` command.
    """
    tracer = _get_tracer(root)
    if tracer is None:
        console.print("[red]Tracer unavailable.[/red]")
        return

    # Load stored summary from tasks table
    try:
        row = tracer.conn.execute(
            "SELECT prompt, summary, started_at FROM tasks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        task_prompt  = row[0] if row else prompt
        stored_sum   = __import__("json").loads(row[1]) if row and row[1] else {}
        task_started = row[2] if row else None
    except Exception:
        task_prompt  = prompt
        stored_sum   = {}
        task_started = None

    file_changes = stored_sum.get("file_changes", [])
    action_log   = stored_sum.get("action_log", [])

    # ── Rich display ───────────────────────────────────────────────────────────
    from rich.panel import Panel  # noqa: PLC0415
    from rich.text  import Text   # noqa: PLC0415

    header = Text()
    header.append("Task Review", style="bold white")
    header.append(f"\n{task_prompt[:120]}", style="dim")
    if task_started:
        from datetime import datetime, timezone  # noqa: PLC0415
        dt = datetime.fromtimestamp(task_started / 1000, tz=timezone.utc)
        header.append(f"\n  started {dt.strftime('%Y-%m-%d %H:%M UTC')}", style="dim")
    console.print(Panel(header, border_style="blue", expand=False))

    # File changes summary
    if file_changes:
        console.print(f"\n[bold] File Changes[/bold]  [dim]({len(file_changes)} file(s))[/dim]\n")
        for fc in file_changes:
            fp  = fc.get("file_path", "?")
            add = fc.get("lines_added", 0)
            rem = fc.get("lines_removed", 0)
            # Recompute the diff on demand from the blob store
            try:
                diff_text = tracer.diff_task(session_id, fp)
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

    # Action log
    notable = [e for e in action_log if e.get("tool") in ("Bash", "Agent") or e.get("outcome") == "denied"]
    if notable:
        console.print(f"[bold] Action Log[/bold]  [dim]({len(notable)} entries)[/dim]\n")
        for e in notable[-20:]:
            inp = e.get("input") or {}
            if e.get("tool") == "Bash":
                cmd = (inp.get("command") or "")[:80]
                ec  = inp.get("exit_code")
                ec_s = f" (exit: {ec})" if ec is not None else ""
                console.print(f"  ● bash: {cmd}{ec_s}")
            elif e.get("tool") == "Agent":
                atype  = inp.get("agent_type") or "agent"
                ph     = (inp.get("prompt_head") or "")[:60]
                console.print(f"  ● agent [{atype}]: {ph}")
            elif e.get("outcome") == "denied":
                console.print(f"  [red]✗ {e.get('tool')} denied: {(e.get('reason') or '')[:80]}[/red]")
        console.print()

    # Operator prompt
    console.print("[bold]Decision:[/bold]  [a] Approve   [d] Dismiss\n", end="")
    try:
        choice = input("  › ").strip().lower()[:1]
    except (EOFError, KeyboardInterrupt):
        choice = ""

    if choice == "d":
        try:
            tracer.set_task_status(session_id, "abandoned")
            console.print("[yellow]Task dismissed.[/yellow]\n")
        except Exception:
            pass
    else:
        try:
            tracer.set_task_status(session_id, "closed")
            console.print("[green]Task approved.[/green]\n")
        except Exception:
            pass


def _check_startup_tasks(root: Path) -> None:
    """
    Called once at REPL startup (and after each ``/new``).

    1. Surfaces ``pending_review`` root tasks and offers approve / dismiss.
    2. Surfaces stale ``open`` tasks (older than 1 hour) and offers to abandon.
    """
    import time  # noqa: PLC0415

    tracer = _get_tracer(root)
    if tracer is None:
        return

    # ── Pending reviews ────────────────────────────────────────────────────────
    try:
        reviews = tracer.get_pending_reviews()
    except Exception:
        reviews = []

    if reviews:
        console.print(
            f"\n[bold yellow]You have {len(reviews)} task(s) awaiting review:[/bold yellow]"
        )
        for i, r in enumerate(reviews, 1):
            from datetime import datetime, timezone  # noqa: PLC0415
            dt = datetime.fromtimestamp(r["started_at"] / 1000, tz=timezone.utc)
            console.print(
                f"  [bold]{i}.[/bold] [dim]{r['session_id'][:12]}…[/dim]"
                f"  \"{r['prompt'][:60]}\"  [dim]({dt.strftime('%H:%M')})[/dim]"
            )
        console.print()
        try:
            choice = input(f"  Review now? [1–{len(reviews)}/skip] › ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "skip"

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(reviews):
                r = reviews[idx]
                _render_review_summary(root, r["session_id"], r["prompt"])
        console.print()

    # ── Stale open tasks ───────────────────────────────────────────────────────
    try:
        cutoff_ms = int((time.time() - _STALE_TASK_THRESHOLD_SECS) * 1000)
        rows = tracer.conn.execute(
            """
            SELECT session_id, prompt, started_at FROM tasks
            WHERE status = 'open'
              AND parent_session_id IS NULL
              AND started_at < ?
            ORDER BY started_at DESC
            """,
            (cutoff_ms,),
        ).fetchall()
        stale = [{"session_id": r[0], "prompt": r[1], "started_at": r[2]} for r in rows]
    except Exception:
        stale = []

    if stale:
        console.print("[bold yellow]Interrupted tasks found (status: open):[/bold yellow]")
        for s in stale:
            from datetime import datetime, timezone  # noqa: PLC0415
            dt = datetime.fromtimestamp(s["started_at"] / 1000, tz=timezone.utc)
            age_h = (time.time() - s["started_at"] / 1000) / 3600
            console.print(
                f"  [dim]{s['session_id'][:12]}…[/dim]"
                f"  \"{s['prompt'][:60]}\""
                f"  [dim](started {age_h:.1f}h ago, never completed)[/dim]"
            )
        console.print()
        try:
            choice = input("  Abandon all interrupted tasks? [y/N] › ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if choice in ("y", "yes"):
            for s in stale:
                try:
                    tracer.set_task_status(s["session_id"], "abandoned")
                except Exception:
                    pass
            console.print(f"[dim]  {len(stale)} task(s) marked abandoned.[/dim]\n")
        console.print()


async def _run_turn(client: ClaudeSDKClient, line: str, verbosity: str) -> None:
    """
    Send *line* to the agent and stream the response to the console.

    Handles all three verbosity modes (verbose / concise / none).
    """
    from claude_agent_sdk import ResultMessage

    if verbosity != "none":
        console.rule(style="dim")

    result_message = None

    await client.query(line)

    if verbosity == "verbose":
        async for message in client.receive_response():
            _render_message(message, "verbose")
            if isinstance(message, ResultMessage):
                result_message = message

    elif verbosity == "concise":
        initial = Text.from_markup("  [dim]Starting…[/dim]")
        with Live(initial, console=console, refresh_per_second=10) as live:
            async for message in client.receive_response():
                _render_message(message, "concise", live=live)
                if isinstance(message, ResultMessage):
                    result_message = message

    else:  # none
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                result_message = message

    if verbosity == "verbose":
        console.rule(style="dim")

    _print_result_summary(result_message, verbosity)


def _run_review_command(root: Path) -> None:
    """
    Handler for the ``/review`` built-in command.

    Lists all ``pending_review`` root tasks and lets the operator pick one
    to approve or dismiss.
    """
    tracer = _get_tracer(root)
    if tracer is None:
        console.print("[red]Tracer unavailable — cannot load pending reviews.[/red]")
        return

    try:
        reviews = tracer.get_pending_reviews()
    except Exception:
        reviews = []

    if not reviews:
        console.print("[dim]No tasks awaiting review.[/dim]\n")
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

    try:
        choice = input(f"  Pick a task [1–{len(reviews)}/cancel] › ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(reviews):
            r = reviews[idx]
            _render_review_summary(root, r["session_id"], r["prompt"])
    else:
        console.print("[dim]Cancelled.[/dim]\n")


async def _run_repl(root: Path, verbosity: str) -> None:
    """
    Main REPL loop.  Keeps one :class:`ClaudeSDKClient` alive across turns
    so the agent accumulates conversation history.

    ``/new`` closes the current client and opens a fresh one, resetting the
    session.  ``/exit``, ``/quit``, or Ctrl-D exit the loop cleanly.
    """
    click.echo(
        click.style("slash⚡", fg="green", bold=True)
        + "\n"
        + click.style(
            "  AI coding agent — powered by Claude.  "
            "All tool calls are policy-checked before execution.\n"
            "  Type a task to get started, /help for commands, or Ctrl-D to exit.",
            fg="white",
            dim=True,
        )
        + "\n"
    )

    # ── Startup: surface pending reviews and stale open tasks ─────────────────
    await anyio.to_thread.run_sync(lambda: _check_startup_tasks(root))

    # prompt_toolkit session — persists history across /new resets
    prompt_session: PromptSession[str] = PromptSession()

    # Outer loop: each iteration represents one "session" (reset by /new).
    while True:
        tracer = _get_tracer(root)
        _session_id: str | None = None
        _task_opened = False

        async with _build_client(root) as client:
            _session_id = getattr(client, "session_id", None)
            session_turns = 0

            # Inner loop: each iteration is one user turn within the session.
            while True:
                # ── read input ───────────────────────────────────────────────
                try:
                    click.echo()
                    line = await anyio.to_thread.run_sync(
                        lambda: prompt_session.prompt(HTML("<ansigreen><b>⚡ </b></ansigreen>"))
                    )
                except (EOFError, KeyboardInterrupt):
                    click.echo()  # newline after ^D / ^C
                    click.echo(click.style("Bye!", fg="cyan"))
                    return

                line = line.strip()
                if not line:
                    continue

                # ── built-in REPL command? ───────────────────────────────────
                if line.startswith(BUILTIN_PREFIX):
                    cmd = line.split()[0].lower()

                    if cmd in ("/exit", "/quit"):
                        click.echo(click.style("Bye!", fg="cyan"))
                        return

                    elif cmd == "/clear":
                        os.system("clear")

                    elif cmd == "/help":
                        click.echo(BUILTIN_HELP)

                    elif cmd == "/status":
                        from .cli import _print_status  # noqa: PLC0415
                        _print_status(root, tail=20)

                    elif cmd == "/review":
                        await anyio.to_thread.run_sync(lambda: _run_review_command(root))

                    elif cmd == "/update":
                        from .init import run_update  # noqa: PLC0415
                        run_update(root)

                    elif cmd == "/new":
                        session_turns = 0
                        click.echo(click.style("↺ New session started.", fg="yellow"))
                        # Surface any new pending reviews after the session ends.
                        await anyio.to_thread.run_sync(lambda: _check_startup_tasks(root))
                        break  # exit inner loop → re-enter outer loop (new client)

                    elif cmd == "/sh":
                        shell_cmd = line[len("/sh"):].strip()
                        if not shell_cmd:
                            click.echo(click.style("Usage: /sh <command>", fg="yellow"))
                        else:
                            subprocess.run(shell_cmd, shell=True)

                    else:
                        click.echo(
                            click.style(
                                f"Unknown command: {cmd}  (try /help)", fg="red"
                            )
                        )
                    continue

                # ── forward to agent ─────────────────────────────────────────
                # Register this REPL session as an open task on the first real
                # user turn (so the prompt is meaningful, not a placeholder).
                if not _task_opened and tracer and _session_id:
                    try:
                        tracer.open_task(_session_id, line)
                        _task_opened = True
                    except Exception:
                        pass

                session_turns += 1
                await _run_turn(client, line, verbosity)

            # Inner loop exited via /new — fall through to outer loop for a
            # fresh client.  Any other exit path returns from the function.


def start_repl(root: Path, verbosity: str | None) -> None:
    """Entry point called from the CLI.

    Reads ``runtime.mode`` from the manifest.  When the mode is ``docker``,
    delegates execution to :class:`~slash.docker_runner.DockerRunner`; the
    REPL session runs inside an isolated container with a TTY.  In local mode
    the REPL runs in-process as before.
    """
    manifest = _load_manifest(root)
    runtime  = manifest.get("runtime", {})

    # SLASH_FORCE_LOCAL is set by DockerRunner when it launches this process
    # inside a container.  It prevents re-entering docker mode when the
    # in-container slash reads the workspace manifest (which still says
    # mode: docker on the host side).
    force_local = os.environ.get("SLASH_FORCE_LOCAL") == "1"

    if not force_local and runtime.get("mode") == "docker":
        from slash.docker_runner import DockerRunner
        _print_runtime_banner(root)
        docker_config = runtime.get("docker", {})
        runner = DockerRunner(docker_config, root)
        runner.run_repl(verbosity=verbosity)
        return

    resolved_verbosity = verbosity or _load_verbosity_config(root)
    anyio.run(_run_repl, root, resolved_verbosity)
