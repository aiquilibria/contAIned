"""Interactive REPL — transparent PTY proxy around the native claude interactive session.

``slash repl`` spawns the ``claude`` CLI in a pseudo-terminal and relays all I/O
between the user's terminal and the claude process.  The proxy intercepts lines that
start with a recognised slash-command prefix *before* they reach claude; everything
else — claude's full native rendering, built-in commands, streaming output, tool
display — passes through completely unmodified.

Architecture
------------

::

    User's terminal (raw mode)
            │ ▲
            │ │  character-by-character passthrough
            ▼ │
      slash PTY proxy  ←── intercepts on Enter keypress only
            │ ▲              checks line buffer vs. _SLASH_COMMANDS
            │ │              match → handled locally, Enter NOT forwarded
            ▼ │              no match → passes through unchanged
      claude process (in PTY slave)
            │ ▲
            │ │  all native: colours, streaming, /help, /compact, etc.
            ▼ │
      Anthropic API

Docker mode
-----------
``docker_runner.py`` is unchanged — it still runs
``docker run -it ... slash:latest repl`` on the host and
``SLASH_FORCE_LOCAL=1`` causes the in-container ``slash repl`` to take this
path instead of re-entering docker mode.  The PTY proxy therefore runs inside
the container, which is why ``/sh`` executes in the container environment
(consistent with the previous SDK-based behaviour).
"""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import termios
import tty
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

# Only /review is intercepted by the PTY proxy — it needs interactive I/O that
# must run in-process.  Everything else is handled natively by Claude Code:
#   /exit /quit → claude's own exit (Ctrl-D also works)
#   /new        → use claude's native /clear to start a fresh conversation
#   /db /status /sh /update → UserPromptSubmit hook (user_prompt_submit.py)
_SLASH_COMMANDS = frozenset({"/review"})


# ── Terminal helpers ───────────────────────────────────────────────────────────

def _sync_winsize(src_fd: int, dst_fd: int) -> None:
    """Copy terminal window size from *src_fd* to *dst_fd* via ioctl."""
    import fcntl
    try:
        buf = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, buf)
    except OSError:
        pass


# ── Slash command implementations ─────────────────────────────────────────────


def _render_review_summary(root: Path, session_id: str, prompt: str) -> tuple[str | None, str | None]:
    """
    Render a stored task review summary and prompt for approve / follow-up.

    Returns
    -------
    ``(session_id, follow_up)``  when the operator typed a follow-up instruction.
    ``(None, None)``             when approved (blank Enter) or skipped.
    """
    import json as _json
    from rich.panel import Panel
    from rich.text import Text

    tracer = _get_tracer(root)
    if tracer is None:
        console.print("[red]Tracer unavailable.[/red]")
        return None, None

    try:
        row = tracer.conn.execute(
            "SELECT prompt, summary, started_at FROM tasks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        task_prompt  = row[0] if row else prompt
        stored_sum   = _json.loads(row[1]) if row and row[1] else {}
        task_started = row[2] if row else None
    except Exception:
        task_prompt  = prompt
        stored_sum   = {}
        task_started = None

    file_changes = stored_sum.get("file_changes", [])
    action_log   = stored_sum.get("action_log", [])

    header = Text()
    header.append("Task Review", style="bold white")
    header.append(f"\n{task_prompt[:120]}", style="dim")
    if task_started:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(task_started / 1000, tz=timezone.utc)
        header.append(f"\n  started {dt.strftime('%Y-%m-%d %H:%M UTC')}", style="dim")
    console.print(Panel(header, border_style="blue", expand=False))

    if file_changes:
        console.print(f"\n[bold] File Changes[/bold]  [dim]({len(file_changes)} file(s))[/dim]\n")
        for fc in file_changes:
            fp  = fc.get("file_path", "?")
            add = fc.get("lines_added", 0)
            rem = fc.get("lines_removed", 0)
            try:
                diff_text  = tracer.diff_task(session_id, fp)
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

    console.print(
        "[bold]Press Enter to approve[/bold] — "
        "or type a follow-up instruction and press Enter"
    )
    try:
        follow_up = input("  ⚡ ").strip()
    except (EOFError, KeyboardInterrupt):
        follow_up = ""

    if follow_up:
        try:
            tracer.set_task_status(session_id, "open")
            console.print("[yellow]Follow-up queued — will be sent as first message.[/yellow]\n")
        except Exception:
            pass
        return session_id, follow_up
    else:
        try:
            tracer.set_task_status(session_id, "closed")
            console.print("[green]Task approved.[/green]\n")
        except Exception:
            pass
        return None, None


def _run_review_command(root: Path) -> None:
    """Handler for the ``/review`` built-in command."""
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
        from datetime import datetime, timezone
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


def _dispatch_slash(line: str, root: Path) -> None:
    """Handle a recognised slash command locally (currently only ``/review``)."""
    cmd = line.split()[0].lower() if line.split() else ""
    if cmd == "/review":
        _run_review_command(root)


# ── Pre-flight checks ──────────────────────────────────────────────────────────

def _pre_launch_checks(root: Path) -> str | None:
    """
    Run pre-flight checks before handing control to the native claude REPL.

    Surfaces ``pending_review`` tasks and lets the operator approve or send
    a follow-up before the new session starts.  Session continuity (resuming
    interrupted sessions) is delegated entirely to claude's native UI.

    Returns
    -------
    ``pending_injection``
        Text to inject as the first message after claude draws its initial
        prompt (used when the operator typed a review follow-up).
        ``None`` if there is nothing to inject.
    """
    tracer = _get_tracer(root)
    if tracer is None:
        return None

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
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(r["started_at"] / 1000, tz=timezone.utc)
            console.print(
                f"  [bold]{i}.[/bold] [dim]{r['session_id'][:12]}…[/dim]"
                f"  \"{r['prompt'][:60]}\"  [dim]({dt.strftime('%H:%M')})[/dim]"
            )
            for fc in (r.get("summary") or {}).get("file_changes", []):
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
                _sid, follow_up = _render_review_summary(root, r["session_id"], r["prompt"])
                if follow_up:
                    return follow_up
        console.print()

    return None


# ── PTY proxy ─────────────────────────────────────────────────────────────────

def _spawn_claude(root: Path) -> tuple[int, int, subprocess.Popen]:
    """
    Spawn ``claude`` in a new PTY slave.

    Returns ``(master_fd, slave_fd, proc)``.  The caller **must** close
    *slave_fd* after calling this function so the master side receives EOF
    when the process exits rather than blocking indefinitely.

    Raises ``SystemExit(1)`` with a helpful message if the ``claude``
    binary cannot be found.
    """
    import pty as _pty

    cmd = ["claude"]

    model = _load_model_config(root)
    if model:
        cmd += ["--model", model]

    master_fd, slave_fd = _pty.openpty()
    _sync_winsize(sys.stdout.fileno(), master_fd)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(root),
            preexec_fn=os.setsid,
            close_fds=True,
        )
    except FileNotFoundError:
        os.close(master_fd)
        os.close(slave_fd)
        console.print(
            "\n[red]Error:[/red] [bold]claude[/bold] not found.\n"
            "Install the Claude Code CLI: [dim]curl -fsSL https://claude.ai/install.sh | bash[/dim]\n"
        )
        raise SystemExit(1)

    return master_fd, slave_fd, proc


def _run_proxy(
    master_fd: int,
    proc: subprocess.Popen,
    root: Path,
    *,
    pending_injection: str | None = None,
) -> str:
    """
    Transparent PTY relay between the user's terminal and the claude process.

    Each character is forwarded immediately in both directions.  On Enter the
    proxy checks the accumulated line buffer against ``_SLASH_COMMANDS``:

    * **Match** — command is handled locally; Enter is *not* forwarded to claude.
    * **No match** — Enter passes through like any other character (the line has
      already been forwarded character-by-character as the user typed it).

    If *pending_injection* is non-empty, the proxy waits for claude to produce
    its initial prompt output, then injects the text as simulated keystrokes.

    Returns
    -------
    ``"exit"``   — clean exit (Ctrl-D, ``/exit``, ``/quit``, or normal claude exit).
    ``"new"``    — user typed ``/new``; outer loop should restart claude.
    ``"crash"``  — claude exited with a non-zero exit code.
    """
    # ── inject pending follow-up once claude has drawn its first output ────────
    if pending_injection:
        r, _, _ = select.select([master_fd], [], [], 8.0)
        if r:
            try:
                data = os.read(master_fd, 4096)
                os.write(sys.stdout.fileno(), data)
            except OSError:
                pass
            import time
            time.sleep(0.15)  # let claude finish drawing the full initial prompt
            os.write(master_fd, pending_injection.encode("utf-8") + b"\r")

    # ── terminal size synchronisation ─────────────────────────────────────────
    _sync_winsize(sys.stdout.fileno(), master_fd)
    old_sigwinch = signal.signal(
        signal.SIGWINCH,
        lambda *_: _sync_winsize(sys.stdout.fileno(), master_fd),
    )

    # ── raw mode ───────────────────────────────────────────────────────────────
    stdin_fd     = sys.stdin.fileno()
    stdout_fd    = sys.stdout.fileno()
    old_settings = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)

    line_buf:  bytes       = b""
    slash_buf: bytes | None = None  # local buffer for lines starting with '/'
    action:    str         = "exit"

    try:
        while proc.poll() is None:
            try:
                r, _, _ = select.select([stdin_fd, master_fd], [], [], 0.05)
            except (ValueError, OSError):
                break

            # ── user → claude ──────────────────────────────────────────────────
            if stdin_fd in r:
                try:
                    ch = os.read(stdin_fd, 256)
                except OSError:
                    break

                # Ctrl-D — let claude handle the exit
                if b"\x04" in ch:
                    try:
                        os.write(master_fd, ch)
                    except OSError:
                        pass
                    break

                if b"\r" in ch or b"\n" in ch:
                    raw_before = ch.split(b"\r")[0].split(b"\n")[0]

                    if slash_buf is not None:
                        # ── resolve locally-buffered slash line ────────────────
                        candidate = (slash_buf + raw_before).decode("utf-8", errors="replace").strip()
                        held      = slash_buf + raw_before
                        slash_buf = None
                        line_buf  = b""

                        cmd_word = candidate.split()[0].lower() if candidate.split() else ""
                        if cmd_word in _SLASH_COMMANDS:
                            # Recognised slash command — was never sent to claude,
                            # so no autocomplete overlay to clean up.
                            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
                            os.write(stdout_fd, b"\r\n")

                            _dispatch_slash(candidate, root)

                            tty.setraw(stdin_fd)

                            # Prod claude into repainting its TUI so it draws
                            # cleanly below our output in the scrollback.
                            try:
                                os.kill(proc.pid, signal.SIGWINCH)
                            except OSError:
                                pass
                            continue

                        # Not a recognised slash command — flush buffered chars
                        # plus Enter to claude as if they had been typed normally.
                        try:
                            os.write(master_fd, held + b"\r")
                        except OSError:
                            break
                        continue

                    else:
                        line_buf = b""
                        try:
                            os.write(master_fd, ch)
                        except OSError:
                            break

                else:
                    if slash_buf is not None:
                        # ── accumulate in local slash buffer ───────────────────
                        if ch == b"\x7f":  # backspace
                            if slash_buf:
                                slash_buf = slash_buf[:-1]
                                os.write(stdout_fd, b"\x08 \x08")  # erase visually
                            if not slash_buf:
                                # Buffer emptied — clear our prompt line and let
                                # claude repaint its own TUI over it.
                                os.write(stdout_fd, b"\x1b[2K\r")
                                slash_buf = None
                                try:
                                    os.kill(proc.pid, signal.SIGWINCH)
                                except OSError:
                                    pass
                        elif len(ch) == 1 and ch[0:1] >= b" ":
                            slash_buf += ch
                            os.write(stdout_fd, ch)  # local echo only
                        else:
                            # Escape / arrow / special key — exit slash mode,
                            # clear our prompt line, flush to claude.
                            os.write(stdout_fd, b"\x1b[2K\r")
                            try:
                                os.write(master_fd, slash_buf + ch)
                            except OSError:
                                break
                            slash_buf = None
                        continue  # never forward to claude while slash-buffering

                    # ── normal (non-slash) input ───────────────────────────────
                    if ch == b"\x7f":  # backspace
                        if line_buf:
                            line_buf = line_buf[:-1]
                    elif not line_buf and ch == b"/":
                        # First character on a fresh line is '/' — start local buffer
                        # so claude never sees the characters and won't show its own
                        # command-completion overlay.  Clear the current line and
                        # draw our own prompt symbol so the user has visual context.
                        slash_buf = b"/"
                        line_buf  = b""
                        os.write(stdout_fd, b"\x1b[2K\r\xe2\x9a\xa1 /")  # ⚡ /
                        continue  # don't forward to claude
                    elif len(ch) == 1 and ch[0:1] >= b" ":
                        line_buf += ch

                    # Forward to claude (everything outside slash-buffer mode)
                    try:
                        os.write(master_fd, ch)
                    except OSError:
                        break

            # ── claude → user ──────────────────────────────────────────────────
            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                    os.write(stdout_fd, data)
                except OSError:
                    break  # claude exited; master_fd closed by kernel

    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
        signal.signal(signal.SIGWINCH, old_sigwinch)

    rc = proc.wait()
    if rc not in (0, -signal.SIGTERM, -signal.SIGHUP):
        action = "crash"

    return action


# ── Entry point ────────────────────────────────────────────────────────────────

def start_repl(root: Path, verbosity: str | None) -> None:
    """
    Entry point called from the CLI.

    **Docker mode:** delegates to ``DockerRunner.run_repl()`` unchanged.
    Inside the container, ``SLASH_FORCE_LOCAL=1`` causes this function to
    take the local path below, running the PTY proxy against the in-container
    ``claude`` binary.

    **Local mode:** validates the workspace, runs pre-flight checks, then
    enters the outer session loop.  Each iteration spawns a native ``claude``
    process in a PTY.  ``/new`` restarts the loop; exit, Ctrl-D, and crashes
    end it.
    """
    # Docker is the only supported runtime.  SLASH_FORCE_LOCAL=1 is set by
    # DockerRunner inside the container so the in-container process runs the
    # PTY proxy directly rather than trying to re-launch docker.
    force_local = os.environ.get("SLASH_FORCE_LOCAL") == "1"

    if not force_local:
        manifest = _load_manifest(root)
        runtime  = manifest.get("runtime", {})
        from slash.docker_runner import DockerRunner
        _print_runtime_banner(root)
        DockerRunner(runtime.get("docker", {}), root).run_repl(verbosity=verbosity)
        return

    # Validate workspace
    missing = _check_initialised(root)
    if missing:
        console.print(
            "\n[red]Error:[/red] workspace not initialised. "
            "Run [bold]slash init[/bold] first.\n"
        )
        for m in missing:
            console.print(f"  [dim]{m}[/dim]")
        console.print()
        raise SystemExit(1)

    # Pre-flight: surface pending reviews
    pending_injection = _pre_launch_checks(root)

    # Outer session loop — each iteration is one claude session.
    while True:
        master_fd, slave_fd, proc = _spawn_claude(root)
        os.close(slave_fd)  # proxy keeps master_fd; slave belongs to proc

        try:
            action = _run_proxy(
                master_fd, proc, root,
                pending_injection=pending_injection,
            )
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
            else:
                proc.wait()

        if action == "crash":
            console.print("\n[yellow]claude exited unexpectedly.[/yellow]\n")

        break
