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
from prompt_toolkit.completion import CompleteEvent, Completer, PathCompleter, WordCompleter
from prompt_toolkit.document import Document
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
  /new          Start a fresh session (old history discarded)
  /status       Show recent audit-log entries
  /review       Review tasks awaiting operator sign-off
  /db [SQL]     Query the tracer DB (omit SQL to list recent tasks)
  /update       Refresh managed hook files from latest templates (same as `slash update`)
  /help         Show this message
  /clear        Clear the terminal
  /sh <cmd>     Run a shell command directly  (e.g. /sh git status)
  /exit         Quit the REPL  (alias: /quit, Ctrl-D)
"""

BUILTIN_COMMANDS = {"/help", "/new", "/status", "/review", "/db", "/update", "/clear", "/exit", "/quit", "/sh"}

# ── Tab completion ──────────────────────────────────────────────────────────────

class _ReplCompleter(Completer):
    """
    Tab-completion for the REPL prompt.

    * Typing ``/`` (or a partial command like ``/he``) completes built-in
      slash commands.
    * Typing ``/sh <partial-path>`` completes filesystem paths for the
      shell-passthrough command.
    * Typing ``/db <partial-sql>`` offers common SQL keyword completions.
    * Everything else (normal agent prompts) is left untouched.
    """

    _command_completer = WordCompleter(
        sorted(BUILTIN_COMMANDS),
        sentence=True,
        match_middle=False,
    )
    _path_completer = PathCompleter(expanduser=True)
    _sql_keywords = WordCompleter(
        [
            "SELECT", "FROM", "WHERE", "ORDER BY", "LIMIT", "GROUP BY",
            "HAVING", "JOIN", "LEFT JOIN", "INNER JOIN", "INSERT INTO",
            "UPDATE", "DELETE", "AND", "OR", "NOT", "NULL", "IS NULL",
            "IS NOT NULL", "COUNT", "SUM", "AVG", "MAX", "MIN",
            "session_id", "status", "prompt", "started_at", "summary",
            "parent_session_id", "tasks", "audit_events", "blobs",
            "snapshots", "baselines",
        ],
        ignore_case=True,
    )

    def get_completions(self, document: Document, complete_event: CompleteEvent):  # type: ignore[override]
        text = document.text_before_cursor

        # ── /sh <path> → filesystem completion ─────────────────────────────
        if text.lstrip().startswith("/sh "):
            # Strip the "/sh " prefix and complete the remainder as a path.
            prefix = text.lstrip()
            path_text = prefix[len("/sh "):]
            path_doc = Document(path_text, cursor_position=len(path_text))
            yield from self._path_completer.get_completions(path_doc, complete_event)
            return

        # ── /db <sql> → SQL keyword completion ─────────────────────────────
        if text.lstrip().startswith("/db "):
            prefix = text.lstrip()
            sql_text = prefix[len("/db "):]
            # Complete only the last whitespace-separated token.
            last_word = sql_text.split()[-1] if sql_text.split() else ""
            sql_doc = Document(last_word, cursor_position=len(last_word))
            yield from self._sql_keywords.get_completions(sql_doc, complete_event)
            return

        # ── starts with / → built-in command completion ────────────────────
        if text.lstrip().startswith("/"):
            stripped = text.lstrip()
            cmd_doc = Document(stripped, cursor_position=len(stripped))
            yield from self._command_completer.get_completions(cmd_doc, complete_event)
            return

        # ── normal agent prompt → no completions ───────────────────────────


# ── Tracer helpers ─────────────────────────────────────────────────────────────


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

    # Operator prompt — blank line = approve; any non-empty text = follow-up
    console.print("[bold]Press Enter to approve[/bold] — or type a follow-up instruction and press Enter")
    try:
        follow_up = input("  ⚡ ").strip()
    except (EOFError, KeyboardInterrupt):
        follow_up = ""

    if follow_up:
        try:
            tracer.set_task_status(session_id, "open")
            console.print("[yellow]Follow-up sent — agent will continue.[/yellow]\n")
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

    Surfaces ``pending_review`` root tasks and offers approve / dismiss.
    """
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
        _STATUS_LABEL = {"new file": "A", "modified": "M", "deleted": "D"}
        _STATUS_STYLE = {"new file": "green", "modified": "yellow", "deleted": "red"}
        for i, r in enumerate(reviews, 1):
            from datetime import datetime, timezone  # noqa: PLC0415
            dt = datetime.fromtimestamp(r["started_at"] / 1000, tz=timezone.utc)
            console.print(
                f"  [bold]{i}.[/bold] [dim]{r['session_id'][:12]}…[/dim]"
                f"  \"{r['prompt'][:60]}\"  [dim]({dt.strftime('%H:%M')})[/dim]"
            )
            file_changes = (r.get("summary") or {}).get("file_changes", [])
            for fc in file_changes:
                ct    = fc.get("change_type", "modified")
                label = _STATUS_LABEL.get(ct, "M")
                style = _STATUS_STYLE.get(ct, "yellow")
                fp    = fc.get("file_path", "?")
                add   = fc.get("lines_added", 0)
                rem   = fc.get("lines_removed", 0)
                console.print(
                    f"      [{style}][bold]{label}[/bold][/{style}]"
                    f"  [cyan]{fp}[/cyan]"
                    f"  [green]+{add}[/green] [red]-{rem}[/red]"
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


class _AbortTurn(Exception):
    """Raised when the operator presses Ctrl+C during an agent turn."""


async def _run_turn(
    client: ClaudeSDKClient,
    line: str,
    verbosity: str,
    on_session_id: object | None = None,
) -> object | None:
    """
    Send *line* to the agent and stream the response to the console.

    Handles all three verbosity modes (verbose / concise / none).

    *on_session_id*, if provided, is called with the session_id string the
    first time any message carrying a ``session_id`` attribute is received.
    This fires early in the stream — before the Stop hook runs — so callers
    can register a task row in the tracer DB in time for the hook's UPDATE.

    Raises :class:`_AbortTurn` when the operator presses Ctrl+C mid-turn so
    the caller can redirect to the task stop / review flow.

    Returns the :class:`~claude_agent_sdk.ResultMessage` (or ``None`` if
    the turn produced no result), so callers can inspect cost/usage data.
    """
    from claude_agent_sdk import ResultMessage

    if verbosity != "none":
        console.rule(style="dim")

    result_message = None
    _sid_fired = False

    def _maybe_fire_sid(message: object) -> None:
        nonlocal _sid_fired
        if _sid_fired or on_session_id is None:
            return
        sid = getattr(message, "session_id", None)
        if sid:
            _sid_fired = True
            on_session_id(sid)  # type: ignore[operator]

    try:
        await client.query(line)

        if verbosity == "verbose":
            async for message in client.receive_response():
                _maybe_fire_sid(message)
                _render_message(message, "verbose")
                if isinstance(message, ResultMessage):
                    result_message = message

        elif verbosity == "concise":
            initial = Text.from_markup("  [dim]Starting…[/dim]")
            with Live(initial, console=console, refresh_per_second=10) as live:
                async for message in client.receive_response():
                    _maybe_fire_sid(message)
                    _render_message(message, "concise", live=live)
                    if isinstance(message, ResultMessage):
                        result_message = message

        else:  # none
            async for message in client.receive_response():
                _maybe_fire_sid(message)
                if isinstance(message, ResultMessage):
                    result_message = message

    except KeyboardInterrupt:
        click.echo()  # newline after ^C
        console.print("\n[yellow]⚠  Turn interrupted — jumping to task review.[/yellow]")
        raise _AbortTurn()

    if verbosity == "verbose":
        console.rule(style="dim")

    _print_result_summary(result_message, verbosity)
    return result_message


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


def _run_db_command(root: Path, query: str) -> None:
    """
    Handler for the ``/db [SQL]`` built-in command.

    Runs *query* against the tracer DB and renders results as a Rich table.
    If *query* is empty, lists the 10 most recent root tasks (same default as
    ``slash db`` with no arguments).
    """
    import json as _json  # noqa: PLC0415
    import sqlite3 as _sqlite3  # noqa: PLC0415

    from rich.table import Table  # noqa: PLC0415

    db_path = root / ".slash" / "tracer.db"
    if not db_path.exists():
        console.print("[red]No tracer.db found.[/red] Run [bold]slash init[/bold] first.")
        return

    if not query:
        query = (
            "SELECT session_id, status, "
            "datetime(started_at/1000,'unixepoch') AS started, "
            "substr(prompt,1,60) AS prompt "
            "FROM tasks WHERE parent_session_id IS NULL "
            "ORDER BY started_at DESC LIMIT 10"
        )

    conn = _sqlite3.connect(str(db_path))
    conn.row_factory = _sqlite3.Row
    try:
        rows = conn.execute(query).fetchall()
    except _sqlite3.Error as exc:
        console.print(f"[red]SQL error:[/red] {exc}")
        return
    finally:
        conn.close()
    if not rows:
        console.print("[dim]No rows.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None)
    for col in rows[0].keys():
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(v) if v is not None else "" for v in row])
    console.print(table)


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
    prompt_session: PromptSession[str] = PromptSession(completer=_ReplCompleter(), complete_while_typing=False)

    # Persistent REPL state — survives /new resets so the status line always
    # reflects the most-recently-known session id and cumulative cost.
    _session_id: str | None = None
    _latest_cost: float | None = None

    # Outer loop: each iteration represents one "session" (reset by /new).
    while True:
        tracer = _get_tracer(root)
        _task_opened = False

        async with _build_client(root) as client:
            session_turns = 0

            # Inner loop: each iteration is one user turn within the session.
            while True:
                # ── read input ───────────────────────────────────────────────
                try:
                    click.echo()
                    # ── status line: shown only once session id is known ──────
                    if _session_id is not None or _latest_cost is not None:
                        sid_label = _session_id or ""
                        cost_label = f"${_latest_cost:.4f}" if _latest_cost is not None else ""
                        try:
                            term_width = os.get_terminal_size().columns
                        except OSError:
                            term_width = 80
                        gap = term_width - len(sid_label) - len(cost_label)
                        click.echo(click.style("─" * term_width, fg="bright_black"))
                        status_line = (
                            click.style(sid_label, fg="bright_black")
                            + (" " * max(gap, 1))
                            + click.style(cost_label, fg="bright_black")
                        )
                        click.echo(status_line)
                    line = await anyio.to_thread.run_sync(
                        lambda: prompt_session.prompt(HTML("<ansigreen><b>⚡ </b></ansigreen>"))
                    )
                except KeyboardInterrupt:
                    click.echo()  # newline after ^C
                    continue  # abort this input line, loop back to prompt
                except EOFError:
                    click.echo()  # newline after ^D
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

                    elif cmd == "/db":
                        await anyio.to_thread.run_sync(
                            lambda: _run_db_command(root, line[len("/db"):].strip())
                        )

                    elif cmd == "/update":
                        from .init import run_update  # noqa: PLC0415
                        run_update(root)

                    elif cmd == "/new":
                        session_turns = 0
                        _latest_cost = None
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
                session_turns += 1
                _first_line = line  # capture for the on_session_id closure

                def _on_sid(sid: str) -> None:
                    """Register the task row as soon as the session_id is known.

                    Called on the first streamed message that carries a
                    session_id — before the Stop hook fires its UPDATE — so
                    the row exists and the summary/narrative are persisted.
                    """
                    nonlocal _session_id, _task_opened
                    _session_id = sid
                    if not _task_opened and tracer:
                        try:
                            tracer.open_task(sid, _first_line)
                            _task_opened = True
                        except Exception:
                            pass

                try:
                    _result = await _run_turn(
                        client, line, verbosity,
                        on_session_id=_on_sid if not _task_opened else None,
                    )
                    _turn_cost = getattr(_result, "total_cost_usd", None)
                    if _turn_cost is not None:
                        _latest_cost = _turn_cost
                    # Keep _session_id in sync from the ResultMessage too, in
                    # case on_session_id never fired (e.g. empty response).
                    _sid = getattr(_result, "session_id", None)
                    if _sid:
                        _session_id = _sid
                except _AbortTurn:
                    # Ctrl+C pressed mid-turn — the SDK context will exit and
                    # the Stop hook (summarizer.py) fires automatically, handling
                    # the approve/dismiss/continue flow just like a normal turn.
                    if not _task_opened:
                        click.echo(click.style("↺ Starting fresh session.", fg="yellow"))
                    break  # exit inner loop → outer loop opens a new client

            # Inner loop exited via /new or abort — fall through to outer loop
            # for a fresh client.  Any other exit path returns from the function.


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
