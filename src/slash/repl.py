"""Interactive REPL — keeps one agent session alive across turns."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread
import click

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.text import Text

from .runner import (
    _build_client,
    _load_verbosity_config,
    _print_result_summary,
    _render_message,
)

console = Console()

BUILTIN_PREFIX = "/"

BUILTIN_HELP = """\
Built-in REPL commands (handled locally, not sent to the agent):
  /new      Start a fresh session (old history discarded)
  /status   Show recent audit-log entries
  /update   Refresh managed hook files from latest templates (same as `slash update`)
  /help     Show this message
  /clear    Clear the terminal
  /sh <cmd> Run a shell command directly  (e.g. /sh git status)
  /exit     Quit the REPL  (alias: /quit, Ctrl-D)
"""

BUILTIN_COMMANDS = {"/help", "/new", "/status", "/update", "/clear", "/exit", "/quit", "/sh"}


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


async def _run_repl(root: Path, verbosity: str) -> None:
    """
    Main REPL loop.  Keeps one :class:`ClaudeSDKClient` alive across turns
    so the agent accumulates conversation history.

    ``/new`` closes the current client and opens a fresh one, resetting the
    session.  ``/exit``, ``/quit``, or Ctrl-D exit the loop cleanly.
    """
    click.echo(
        click.style("slash repl", fg="cyan", bold=True)
        + "  —  type /help for built-in commands, Ctrl-D to exit\n"
    )

    # Outer loop: each iteration represents one "session" (reset by /new).
    while True:
        async with _build_client(root) as client:
            session_turns = 0

            # Inner loop: each iteration is one user turn within the session.
            while True:
                # ── read input ───────────────────────────────────────────────
                try:
                    line = await anyio.to_thread.run_sync(
                        lambda: input(click.style("slash⚡ ", fg="green", bold=True))
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
                        from .cli import _print_status
                        _print_status(root, tail=20)

                    elif cmd == "/update":
                        from .init import run_update
                        run_update(root)

                    elif cmd == "/new":
                        session_turns = 0
                        click.echo(click.style("↺ New session started.", fg="yellow"))
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
                await _run_turn(client, line, verbosity)

            # Inner loop exited via /new — fall through to outer loop for a
            # fresh client.  Any other exit path returns from the function.


def start_repl(root: Path, verbosity: str | None) -> None:
    """Entry point called from the CLI."""
    resolved_verbosity = verbosity or _load_verbosity_config(root)
    anyio.run(_run_repl, root, resolved_verbosity)
