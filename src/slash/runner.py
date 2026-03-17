"""
slash run — invoke the agent on a single task.

Validates that the workspace is initialised, then starts the Agent SDK.
Agent config (verbosity, thinking, model) is read from ``.slash/manifest.yaml``
(falls back to ``.slash/policy/manifest.yaml`` for backwards compatibility).
Policy is enforced entirely by the PreToolUse / PostToolUse / Stop hooks;
the ``canUseTool`` callback handles ``AskUserQuestion`` interactively and
prompts the operator for anything not already allowed by ``settings.json``.

When ``runtime.mode`` is ``docker`` in the manifest, execution is delegated
to :class:`slash.docker_runner.DockerRunner` instead of running the agent
in-process.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
import yaml
from pathlib import Path

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient, PermissionResult, ThinkingConfigDisabled, ThinkingConfigEnabled
    from claude_agent_sdk.types import ToolPermissionContext

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text

console = Console()


def _load_thinking_config(root: Path) -> ThinkingConfigEnabled | ThinkingConfigDisabled | None:
    """
    Read ``agent.thinking`` from ``.slash/manifest.yaml`` and return the
    appropriate thinking config object:

    - ``ThinkingConfigEnabled``  when ``thinking.enabled: true``
    - ``ThinkingConfigDisabled`` when ``thinking.enabled: false`` and the SDK
      exports that type (Claude Agent SDK ≥ 0.x)
    - ``None``                   as a last-resort fallback when the disabled
      type is not available in the installed SDK version; the caller omits the
      ``thinking`` kwarg entirely in that case rather than passing ``None``,
      which the SDK could interpret as "no preference / use model default".
    """
    thinking_cfg = _load_manifest(root).get("agent", {}).get("thinking", {})

    if thinking_cfg.get("enabled", False):
        from claude_agent_sdk import ThinkingConfigEnabled
        budget = int(thinking_cfg.get("budget_tokens", 1024))
        return ThinkingConfigEnabled(type="enabled", budget_tokens=budget)

    # Explicitly disabled — use the SDK's own disabled type so the intent is
    # unambiguous.  Passing ``thinking=None`` risks the SDK interpreting it as
    # "no preference" and enabling thinking for models that default to it.
    try:
        from claude_agent_sdk import ThinkingConfigDisabled
        return ThinkingConfigDisabled(type="disabled")
    except ImportError:
        # Older SDK build without a disabled type — caller will omit the kwarg.
        return None


def _load_verbosity_config(root: Path) -> str:
    """
    Read ``agent.verbosity`` from ``.slash/manifest.yaml``.

    Returns one of:
      - ``"verbose"``  — full streaming output (tool calls, results, thinking); **default**
      - ``"concise"``  — single updating status line showing the current tool call
      - ``"none"``     — no intermediate output; only the final result is printed
    """
    value = _load_manifest(root).get("agent", {}).get("verbosity", "verbose")
    if value not in ("verbose", "concise", "none"):
        return "verbose"
    return value


def _load_model_config(root: Path) -> str | None:
    """
    Read ``agent.model`` from ``.slash/manifest.yaml``.

    Returns the model string (e.g. ``"claude-sonnet-4-6"``), or ``None`` if not
    set — in which case the Agent SDK uses its own default.
    """
    return _load_manifest(root).get("agent", {}).get("model") or None


def _check_initialised(root: Path) -> list[str]:
    """Return a list of missing paths that indicate init has not been run."""
    required = [
        root / ".slash" / "hooks" / "restrict_writes.py",
        root / ".slash" / "hooks" / "audit.py",
        root / ".slash" / "hooks" / "qa.py",
        root / ".claude" / "settings.json",
    ]
    missing = [str(p.relative_to(root)) for p in required if not p.exists()]

    # Accept either new or legacy manifest path
    manifest_new = root / ".slash" / "manifest.yaml"
    manifest_old = root / ".slash" / "policy" / "manifest.yaml"
    if not manifest_new.exists() and not manifest_old.exists():
        missing.append(".slash/manifest.yaml")

    return missing


def _tool_input_summary(name: str, input: dict[str, Any]) -> str:
    """Return a short human-readable summary of a tool call's input."""
    # Common single-field tools
    for key in ("command", "file_path", "path", "pattern", "query", "prompt", "question"):
        if key in input:
            value = str(input[key])
            # Trim long values
            if len(value) > 80:
                value = value[:77] + "…"
            return value
    # Fallback: show all keys as key=value, trimmed
    parts = [f"{k}={str(v)[:40]}" for k, v in list(input.items())[:3]]
    summary = "  ".join(parts)
    if len(summary) > 80:
        summary = summary[:77] + "…"
    return summary or "(no input)"


def _load_manifest(root: Path) -> dict[str, Any]:
    """
    Load and return the parsed manifest, or an empty dict if missing.

    Checks ``.slash/manifest.yaml`` first (new location), then falls back to
    ``.slash/policy/manifest.yaml`` (legacy location) for backwards
    compatibility with workspaces initialised before the path migration.
    """
    new_path = root / ".slash" / "manifest.yaml"
    old_path = root / ".slash" / "policy" / "manifest.yaml"
    manifest_path = new_path if new_path.exists() else old_path
    try:
        return yaml.safe_load(manifest_path.read_text()) or {}
    except FileNotFoundError:
        return {}


def _build_client(root: Path, *, resume: str | None = None) -> ClaudeSDKClient:
    """
    Validate the workspace and return a ``ClaudeSDKClient`` instance.

    The returned client is intended to be used as an async context manager::

        async with _build_client(root) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                ...

    Raises ``SystemExit(1)`` if the workspace has not been initialised.

    Policy enforcement
    ------------------
    Policy (secrets, workspace boundaries, bash restrictions) is enforced by
    the PreToolUse hooks before ``can_use_tool`` is ever reached.  Allow rules
    in ``settings.json`` ``permissions.allow`` are enforced natively by the SDK.
    The ``can_use_tool`` callback below handles ``AskUserQuestion`` interactively
    and prompts the operator for anything not already covered by ``settings.json``.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny

    missing = _check_initialised(root)
    if missing:
        console.print("\n[red]Error:[/red] workspace not initialised. Run [bold]slash init[/bold] first.\n")
        console.print("Missing:")
        for m in missing:
            console.print(f"  [dim]{m}[/dim]")
        console.print()
        raise SystemExit(1)

    async def can_use_tool(name: str, input_data: dict[str, Any], context: ToolPermissionContext) -> PermissionResult:
        """
        Two responsibilities, in order:

        1. AskUserQuestion — intercepted for interactive Q&A; never reaches the SDK.
        2. Operator prompt — anything not already allowed by settings.json surfaces
           here for interactive approval; default answer is deny.

        Policy (secrets, workspace, bash restrictions) is enforced by the PreToolUse
        hooks before this callback is reached.  Allow rules are enforced upstream by
        the SDK via settings.json and never reach this callback.
        """
        # ── AskUserQuestion: render interactively, feed answer back ───────────
        if name == "AskUserQuestion":
            questions = input_data.get("questions", [])
            if not questions:
                questions = [
                    {
                        "question": input_data.get("question", input_data.get("prompt", "")),
                        "options": [
                            {"label": str(o), "description": ""}
                            for o in input_data.get("options", [])
                        ],
                        "multiSelect": False,
                    }
                ]

            collected: list[str] = []
            for q in questions:
                text = q.get("question", "")
                opts: list[dict] = q.get("options", [])
                multi: bool = q.get("multiSelect", False)

                console.print(f"\n  [bold yellow]?[/bold yellow] [yellow]{escape(text)}[/yellow]")
                if opts:
                    for i, opt in enumerate(opts, 1):
                        label = opt.get("label", str(opt))
                        desc = opt.get("description", "")
                        if desc:
                            console.print(
                                f"    [dim]{i}.[/dim] [bold]{escape(label)}[/bold]"
                                f"  [dim]{escape(desc)}[/dim]"
                            )
                        else:
                            console.print(f"    [dim]{i}.[/dim] {escape(label)}")
                    if multi:
                        console.print("  [dim](multiple selections allowed, e.g. 1,3)[/dim]")
                console.print()

                answer = await anyio.to_thread.run_sync(
                    lambda: input("  Your answer: ").strip()
                )
                console.print()
                collected.append(f"Q: {text}\nA: {answer}")

            return PermissionResultDeny(
                behavior="deny",
                message="User answered:\n" + "\n\n".join(collected),
                interrupt=False,
            )

        # ── Not in allow list — ask the operator ─────────────────────────────
        summary = _tool_input_summary(name, input_data)
        console.print(
            f"\n  [bold yellow]?[/bold yellow] [yellow]Not in policy — approve?[/yellow]\n"
            f"    [bold]{escape(name)}[/bold]  [dim]{escape(summary)}[/dim]\n"
        )
        answer = await anyio.to_thread.run_sync(
            lambda: input("  Allow? [y/N]: ").strip().lower()
        )
        console.print()

        if answer in ("y", "yes"):
            return PermissionResultAllow(behavior="allow")
        return PermissionResultDeny(
            behavior="deny",
            message=f"Operator denied: {name}({summary})",
            interrupt=False,
        )

    model          = _load_model_config(root)
    thinking_cfg   = _load_thinking_config(root)

    # Build options incrementally so Pyright can track the concrete types of
    # optional fields rather than seeing opaque conditional dict spreads.
    options = ClaudeAgentOptions(
        setting_sources=["project"],
        allowed_tools=[
            "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Bash",
            "AskUserQuestion",
        ],
        cwd=str(root),
        can_use_tool=can_use_tool,
    )

    # When resuming, continue the existing conversation rather than starting
    # a new session; the agent keeps its full history.
    if resume is not None:
        options.resume = resume

    # Model — sourced from agent.model in .slash/manifest.yaml.
    # Omitted entirely when not set so the SDK keeps its own default.
    if model is not None:
        options.model = model

    # Extended thinking — sourced from agent.thinking in manifest.yaml.
    # Omitted entirely only when ThinkingConfigDisabled is unavailable in
    # the installed SDK; in that case we cannot express an explicit disable
    # and the SDK will apply its own default.  When the type IS available,
    # we always pass an explicit enabled or disabled object so the manifest
    # setting is honoured precisely.
    if thinking_cfg is not None:
        options.thinking = thinking_cfg

    return ClaudeSDKClient(options)


def _render_message(message: object, verbosity: str, live: Live | None = None) -> None:
    """
    Render one SDK response message to the console.

    Parameters
    ----------
    message:
        An SDK message object (``AssistantMessage``, ``UserMessage``,
        ``ResultMessage``, …).
    verbosity:
        One of ``"verbose"``, ``"concise"``, or ``"none"``.
    live:
        A :class:`rich.live.Live` instance, required when *verbosity* is
        ``"concise"`` so tool-call updates can overwrite the status line.
        Ignored for other verbosity levels.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    if verbosity == "verbose":
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ThinkingBlock) and block.thinking.strip():
                    console.print(f"[dim italic]{escape(block.thinking)}[/dim italic]")

                elif isinstance(block, TextBlock) and block.text.strip():
                    console.print(Markdown(block.text))

                elif isinstance(block, ToolUseBlock):
                    if block.name == "AskUserQuestion":
                        continue
                    summary = _tool_input_summary(block.name, block.input)
                    console.print(
                        f"  [bold cyan]→[/bold cyan] [cyan]{escape(block.name)}[/cyan]"
                        f"  [dim]{escape(summary)}[/dim]"
                    )

        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    if block.is_error:
                        raw = block.content if isinstance(block.content, str) else str(block.content or "")
                        first_line = raw.splitlines()[0] if raw else "error"
                        if first_line.startswith("User answered:"):
                            return
                        if len(first_line) > 80:
                            first_line = first_line[:77] + "…"
                        console.print(f"  [bold red]✗[/bold red] [dim red]{escape(first_line)}[/dim red]")
                    else:
                        raw = block.content if isinstance(block.content, str) else str(block.content or "")
                        first_line = raw.splitlines()[0] if raw else ""
                        if len(first_line) > 80:
                            first_line = first_line[:77] + "…"
                        display = first_line or f"{len(raw)} chars"
                        console.print(f"  [bold green]✓[/bold green] [dim]{escape(display)}[/dim]")

    elif verbosity == "concise" and live is not None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    if block.name == "AskUserQuestion":
                        continue
                    summary = _tool_input_summary(block.name, block.input)
                    live.update(Text.from_markup(
                        f"  [bold cyan]→[/bold cyan] [cyan]{escape(block.name)}[/cyan]"
                        f"  [dim]{escape(summary)}[/dim]"
                    ))

        elif isinstance(message, ResultMessage):
            live.update(Text(""))  # clear the status line before exit


def _print_result_summary(result_message: object, verbosity: str) -> None:
    """Print the cost/usage footer after a completed turn."""
    from claude_agent_sdk import ResultMessage

    if result_message is None:
        return

    if not isinstance(result_message, ResultMessage):
        return

    if result_message.subtype == "success":
        console.print("\n[green]✓ Task complete[/green]")
    else:
        console.print(f"\n[yellow]! Task ended: {escape(result_message.subtype)}[/yellow]")

    if verbosity == "none" and hasattr(result_message, "result") and result_message.result:
        console.print(f"\n[dim]{escape(result_message.result)}[/dim]")

    cost_parts: list[str] = []

    cost = getattr(result_message, "total_cost_usd", None)
    if cost is not None:
        cost_parts.append(f"[bold]cost:[/bold] [green]${cost:.4f}[/green]")

    usage = getattr(result_message, "usage", None) or {}
    input_tokens  = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cache_read    = usage.get("cache_read_input_tokens")
    cache_create  = usage.get("cache_creation_input_tokens")
    # Some CLI/SDK versions aggregate all token consumption into a single
    # ``total_tokens`` counter (matching the ``TaskUsage`` shape) rather than
    # reporting separate input/output counts.  Use it as a fallback so we
    # always display a meaningful number instead of nothing — or instead of a
    # suspiciously small figure that merely counts API invocations.
    total_tokens  = usage.get("total_tokens")
    input_token_count = 0

    token_parts: list[str] = []
    if input_tokens is not None:
        input_token_count += input_tokens
        # token_parts.append(f"in {input_tokens:,}")
    if cache_read:
        input_token_count += cache_read
        # token_parts.append(f"cache-read {cache_read:,}")
    if cache_create:
        input_token_count += cache_create
        # token_parts.append(f"cache-write {cache_create:,}")
        token_parts.append(f"in {input_token_count:,}")
    if output_tokens is not None:
        token_parts.append(f"out {output_tokens:,}")
    if input_tokens is None and output_tokens is None and total_tokens is not None:
        token_parts.append(f"total {total_tokens:,}")
    if token_parts:
        cost_parts.append(f"[bold]tokens:[/bold] [dim]{' · '.join(token_parts)}[/dim]")

    turns = getattr(result_message, "num_turns", None)
    if turns is not None:
        cost_parts.append(f"[bold]turns:[/bold] [dim]{turns}[/dim]")

    duration_ms = getattr(result_message, "duration_ms", None)
    if duration_ms is not None:
        secs = duration_ms / 1000
        cost_parts.append(f"[bold]time:[/bold] [dim]{secs:.1f}s[/dim]")

    if cost_parts:
        console.print("\n  " + "  ·  ".join(cost_parts))


def _print_runtime_banner(root: Path) -> None:
    """Print a short runtime info line when starting a session."""
    manifest = _load_manifest(root)
    runtime = manifest.get("runtime", {})
    mode = runtime.get("mode", "local")
    if mode == "docker":
        image = runtime.get("docker", {}).get("image", "slash:latest")
        console.print(f"[dim][slash] runtime: docker ({image})[/dim]")
    else:
        console.print("[dim][slash] runtime: local[/dim]")
    console.print(f"[dim][slash] workspace: {root}[/dim]\n")


def _get_tracer(root: Path):
    """Return a :class:`~slash.tracer.SlashTracer` for *root*, or ``None`` if unavailable."""
    try:
        from slash.tracer import SlashTracer  # noqa: PLC0415
        return SlashTracer(str(root / ".slash" / "tracer.db"))
    except Exception:
        return None


def _run_summarizer(session_id: str, root: Path, tracer: Any) -> None:
    """
    Build and render the task summary in-process, then prompt the operator
    to approve, dismiss, or continue.

    Called directly from _run_task_streaming after the agent finishes — no
    subprocess involved, so there is no risk of re-triggering the hook chain.

    Operator choices:
      [a] Approve  → set status = closed  (normal completion)
      [d] Dismiss  → set status = abandoned
      [c] Continue → set status = open; raises _ContinueSignal so the caller
                     can give the agent another turn.
    """
    import time  # noqa: PLC0415

    # ── Defensive child check ──────────────────────────────────────────────────
    open_children: list[str] = []
    for _attempt in range(3):
        try:
            rows = tracer.conn.execute(
                "SELECT session_id FROM tasks WHERE parent_session_id = ? AND status = 'open'",
                (session_id,),
            ).fetchall()
            open_children = [r[0] for r in rows]
        except Exception:
            open_children = []
        if not open_children:
            break
        time.sleep(0.2)

    # ── Collect file diffs ─────────────────────────────────────────────────────
    try:
        touched_files = tracer.list_touched_files(session_id)
    except Exception:
        touched_files = []

    # Build a lookup of write-tool audit events per file path (for reasons).
    try:
        all_audit = tracer.recent_audit_events(session_id, limit=500)
        write_events_by_file: dict[str, list[dict]] = {}
        for ev in reversed(all_audit):
            if ev["tool"] in ("Write", "Edit", "MultiEdit") and ev.get("input"):
                fp = ev["input"].get("file_path") or ""
                if fp:
                    write_events_by_file.setdefault(fp, []).append(ev)
                for mfp in (ev["input"].get("file_paths") or []):
                    write_events_by_file.setdefault(mfp, []).append(ev)
    except Exception:
        write_events_by_file = {}

    # Resolve session tree once for baseline lookups.
    try:
        tree_ids = tracer.tree_session_ids(session_id)
        tree_ph  = ",".join("?" * len(tree_ids)) if tree_ids else "'__none__'"
    except Exception:
        tree_ids = []
        tree_ph  = "'__none__'"

    file_diffs: list[dict] = []
    for file_path in touched_files:
        try:
            diff_text = tracer.diff_task(session_id, file_path)
            if not diff_text:
                continue
            lines_added   = sum(1 for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
            lines_removed = sum(1 for ln in diff_text.splitlines() if ln.startswith("-") and not ln.startswith("---"))

            # Determine change type from the earliest baseline pre_hash.
            change_type = "modified"
            try:
                if tree_ids:
                    bl = tracer.conn.execute(
                        f"SELECT pre_hash FROM baselines WHERE file_path = ? AND session_id IN ({tree_ph}) ORDER BY captured_at ASC LIMIT 1",
                        [file_path, *tree_ids],
                    ).fetchone()
                    if bl is not None:
                        change_type = "new file" if bl[0] is None else "modified"
            except Exception:
                pass

            # Build a short human-readable reason from write-tool events for this file.
            file_write_evs = write_events_by_file.get(file_path, [])
            if file_write_evs:
                tools_used = list(dict.fromkeys(e["tool"] for e in file_write_evs))
                reason = ", ".join(t.lower() for t in tools_used)
            else:
                reason = change_type

            file_diffs.append({
                "file_path":     file_path,
                "diff":          diff_text,
                "lines_added":   lines_added,
                "lines_removed": lines_removed,
                "change_type":   change_type,
                "reason":        reason,
            })
        except Exception:
            pass

    # ── Build action log ───────────────────────────────────────────────────────
    try:
        raw_events = tracer.recent_audit_events(session_id, limit=200)
        action_log = [
            e for e in reversed(raw_events)
            if e["tool"] in ("Bash", "Agent") or e["outcome"] == "denied"
        ]
    except Exception:
        action_log = []

    # ── Look up task prompt ────────────────────────────────────────────────────
    try:
        row = tracer.conn.execute(
            "SELECT prompt, started_at FROM tasks WHERE session_id = ?", (session_id,)
        ).fetchone()
        task_prompt  = row[0] if row else "(unknown)"
        task_started = row[1] if row else None
    except Exception:
        task_prompt  = "(unknown)"
        task_started = None

    # ── Store summary ──────────────────────────────────────────────────────────
    summary = {
        "file_changes": [
            {
                "file_path":     d["file_path"],
                "lines_added":   d["lines_added"],
                "lines_removed": d["lines_removed"],
                "change_type":   d["change_type"],
                "reason":        d["reason"],
            }
            for d in file_diffs
        ],
        "action_log":          action_log,
        "incomplete_children": open_children,
    }
    try:
        tracer.set_task_status(session_id, "pending_review", summary=summary)
    except Exception:
        pass

    # ── Render to terminal ─────────────────────────────────────────────────────
    _STATUS_LABEL = {"new file": "A", "modified": "M", "deleted": "D"}
    _STATUS_STYLE = {"new file": "green", "modified": "yellow", "deleted": "red"}

    from rich.panel import Panel   # noqa: PLC0415
    from rich.text  import Text    # noqa: PLC0415

    # Header
    started_str = ""
    if task_started:
        from datetime import datetime, timezone  # noqa: PLC0415
        dt = datetime.fromtimestamp(task_started / 1000, tz=timezone.utc)
        started_str = "  started " + dt.strftime("%Y-%m-%d %H:%M UTC")

    header = Text()
    header.append("Task Review", style="bold white")
    header.append("\n")
    header.append(task_prompt[:120], style="dim")
    if started_str:
        header.append("\n" + started_str, style="dim")
    if open_children:
        header.append(
            "\n[!] " + str(len(open_children)) + " sub-agent(s) still open — diffs may be incomplete",
            style="yellow",
        )
    console.print(Panel(header, border_style="blue", expand=False))

    # ── Changes — git-status-like ──────────────────────────────────────────────
    if file_diffs:
        console.print("\n[bold] Changes[/bold]  [dim](" + str(len(file_diffs)) + " file(s))[/dim]\n")
        for fd in file_diffs:
            ct    = fd.get("change_type", "modified")
            label = _STATUS_LABEL.get(ct, "M")
            style = _STATUS_STYLE.get(ct, "yellow")
            stat_str = (
                "[green]+" + str(fd["lines_added"]) + "[/green]"
                "  [red]-" + str(fd["lines_removed"]) + "[/red]"
            )
            reason_str = fd.get("reason", "")
            status_line = (
                "  [" + style + "][bold]" + label + "[/bold][/" + style + "]"
                "  [bold cyan]" + escape(fd["file_path"]) + "[/bold cyan]"
                "  " + stat_str
            )
            if reason_str and reason_str != ct:
                status_line += "  [dim](" + escape(reason_str) + ")[/dim]"
            console.print(status_line)

        console.print("")
        for fd in file_diffs:
            console.print("[dim]──[/dim] [bold cyan]" + escape(fd["file_path"]) + "[/bold cyan]")
            diff_text_obj = Text()
            for line in fd["diff"].splitlines()[:200]:
                if line.startswith("+++") or line.startswith("---"):
                    diff_text_obj.append(line + "\n", style="dim")
                elif line.startswith("@@"):
                    diff_text_obj.append(line + "\n", style="cyan")
                elif line.startswith("+"):
                    diff_text_obj.append(line + "\n", style="green")
                elif line.startswith("-"):
                    diff_text_obj.append(line + "\n", style="red")
                else:
                    diff_text_obj.append(line + "\n", style="")
            if len(fd["diff"].splitlines()) > 200:
                diff_text_obj.append("  … (diff truncated)\n", style="dim")
            console.print(diff_text_obj)
    else:
        console.print("\n[dim] No file changes recorded.[/dim]\n")

    # ── Task Completion ────────────────────────────────────────────────────────
    if file_diffs:
        new_files = [fd for fd in file_diffs if fd.get("change_type") == "new file"]
        mod_files = [fd for fd in file_diffs if fd.get("change_type") == "modified"]
        del_files = [fd for fd in file_diffs if fd.get("change_type") == "deleted"]
        parts: list[str] = []
        if new_files:
            parts.append(str(len(new_files)) + " file(s) created")
        if mod_files:
            parts.append(str(len(mod_files)) + " file(s) modified")
        if del_files:
            parts.append(str(len(del_files)) + " file(s) deleted")
        total_added   = sum(fd["lines_added"]   for fd in file_diffs)
        total_removed = sum(fd["lines_removed"] for fd in file_diffs)
        change_summary = ", ".join(parts) if parts else str(len(file_diffs)) + " file(s) changed"
        console.print("[bold] Task Completion[/bold]\n")
        console.print(
            "  The task [dim]\"" + escape(task_prompt[:80]) + "\"[/dim] is complete.\n"
            "  [green]" + change_summary + "[/green]  "
            "([green]+" + str(total_added) + "[/green] / [red]-" + str(total_removed) + "[/red] lines total).\n"
        )
        if new_files:
            console.print("  [bold]New files:[/bold]")
            for fd in new_files:
                console.print("    [green]" + escape(fd["file_path"]) + "[/green]")
        if mod_files:
            console.print("  [bold]Modified:[/bold]")
            for fd in mod_files:
                console.print("    [yellow]" + escape(fd["file_path"]) + "[/yellow]  [dim](" + escape(fd.get("reason", "")) + ")[/dim]")
        if del_files:
            console.print("  [bold]Deleted:[/bold]")
            for fd in del_files:
                console.print("    [red]" + escape(fd["file_path"]) + "[/red]")
        console.print("")

    # ── Action log ────────────────────────────────────────────────────────────
    if action_log:
        bash_count   = sum(1 for e in action_log if e["tool"] == "Bash")
        agent_count  = sum(1 for e in action_log if e["tool"] == "Agent")
        denied_count = sum(1 for e in action_log if e["outcome"] == "denied")
        aparts: list[str] = []
        if bash_count:
            aparts.append(str(bash_count) + " bash")
        if agent_count:
            aparts.append(str(agent_count) + " sub-agent")
        if denied_count:
            aparts.append(str(denied_count) + " denied")
        console.print("[bold] Action Log[/bold]  [dim](" + ", ".join(aparts) + ")[/dim]\n")
        for e in action_log[-30:]:
            inp = e.get("input") or {}
            if e["tool"] == "Bash":
                cmd = escape((inp.get("command") or "")[:80])
                ec  = inp.get("exit_code")
                ec_str = (" (exit: " + str(ec) + ")") if ec is not None else ""
                style = "red" if e["outcome"] == "denied" else "default"
                console.print("  [" + style + "]● bash: " + cmd + ec_str + "[/" + style + "]")
            elif e["tool"] == "Agent":
                atype  = inp.get("agent_type") or "agent"
                prompt = escape((inp.get("prompt_head") or "")[:60])
                console.print("  ● agent [" + escape(str(atype)) + "]: " + prompt)
            elif e["outcome"] == "denied":
                rsn = escape((e.get("reason") or "")[:80])
                console.print("  [red]✗ " + escape(e["tool"]) + " denied: " + rsn + "[/red]")
        console.print("")

    # ── Operator prompt ────────────────────────────────────────────────────────
    console.print("")
    console.print("[bold yellow]⏸  Awaiting your sign-off — task is NOT closed yet.[/bold yellow]")
    console.print("[bold]Review:[/bold]  [a] Approve   [d] Dismiss   [c] Continue with new instruction")
    console.print("[dim](Press a / d / c then Enter)[/dim] ", end="")

    # Read from /dev/tty so we bypass any stdin pipe.
    choice = ""
    non_interactive = False
    try:
        with open("/dev/tty") as tty:
            raw = tty.readline().strip()
        choice = raw[:1].lower() if raw else ""
    except Exception:
        non_interactive = True
        choice = "a"

    if not non_interactive and choice not in ("a", "d", "c"):
        try:
            console.print("\n  Unrecognised — enter a (approve), d (dismiss), or c (continue): ", end="")
            with open("/dev/tty") as tty:
                raw = tty.readline().strip()
            choice = raw[:1].lower() if raw else "d"
        except Exception:
            choice = "d"
        if choice not in ("a", "d", "c"):
            choice = "d"

    console.print("")  # newline after inline prompt

    if choice == "d":
        console.print("  Dismissed — task marked abandoned.")
        try:
            tracer.set_task_status(session_id, "abandoned")
        except Exception:
            pass
        return

    if choice == "c":
        follow_up = ""
        try:
            console.print("  Follow-up instruction (required — press Enter when done):")
            console.print("  > ", end="")
            with open("/dev/tty") as tty:
                follow_up = tty.readline().strip()
        except Exception:
            pass

        if not follow_up:
            console.print("  No instruction provided — approving instead.")
            try:
                tracer.set_task_status(session_id, "closed")
            except Exception:
                pass
            return

        console.print("  Continuing with new instruction.")
        try:
            tracer.set_task_status(session_id, "open")
        except Exception:
            pass
        raise _ContinueSignal("Operator follow-up: " + follow_up)

    # Approve
    console.print("  Approved — task closed.")
    try:
        tracer.set_task_status(session_id, "closed")
    except Exception:
        pass


class _ContinueSignal(Exception):
    """Raised by _run_summarizer when the operator chooses 'continue'."""
    def __init__(self, follow_up: str) -> None:
        super().__init__(follow_up)
        self.follow_up = follow_up


async def _run_task_streaming(prompt: str, root: Path, verbosity: str = "verbose") -> None:
    """
    Stream agent output with verbosity controlled by the manifest's
    ``agent.verbosity`` setting:

    - ``verbose``  — full output: thinking blocks, text, every tool call and result
    - ``concise``  — single updating status line with the current tool call
    - ``none``     — silent during execution; only the final result is shown
    """
    from claude_agent_sdk import ResultMessage

    # ── Header ────────────────────────────────────────────────────────────────
    if verbosity != "none":
        console.print(f"\n[bold]Task:[/bold] {escape(prompt)}\n")
        console.rule(style="dim")

    tracer = _get_tracer(root)
    _task_session_id: str | None = None
    result_message = None

    try:
        async with _build_client(root) as client:
            # Register the task now that the SDK has assigned a session_id.
            _task_session_id = getattr(client, "session_id", None)
            if tracer and _task_session_id:
                tracer.open_task(_task_session_id, prompt)

            await client.query(prompt)

            # ── verbose ───────────────────────────────────────────────────────
            if verbosity == "verbose":
                async for message in client.receive_response():
                    _render_message(message, "verbose")
                    if isinstance(message, ResultMessage):
                        result_message = message

            # ── concise ───────────────────────────────────────────────────────
            elif verbosity == "concise":
                initial = Text.from_markup("  [dim]Starting…[/dim]")
                with Live(initial, console=console, refresh_per_second=10) as live:
                    async for message in client.receive_response():
                        _render_message(message, "concise", live=live)
                        if isinstance(message, ResultMessage):
                            result_message = message

            # ── none ──────────────────────────────────────────────────────────
            else:
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage):
                        result_message = message

            # ── Footer — printed before the SDK context exits (QA hook fires) ──
            if verbosity == "verbose":
                console.rule(style="dim")

            _print_result_summary(result_message, verbosity)

            console.print(f"\nTrace: [dim].slash/tracer.db[/dim]\n")

        # SDK context has exited; QA hook has already run.  Now run the
        # summarizer in-process so it never triggers the hook chain again.
        if tracer and _task_session_id:
            # Narrow to str — the `if _task_session_id` guard above guarantees
            # this is not None, but Pyright needs a local binding to see it.
            task_session_id: str = _task_session_id
            while True:
                try:
                    _run_summarizer(task_session_id, root, tracer)
                    break  # approved or dismissed — done
                except _ContinueSignal as sig:
                    # Operator asked for another turn.  Resume the *same*
                    # session so the agent retains its full conversation
                    # history; no new task row is opened.
                    async with _build_client(root, resume=task_session_id) as client:
                        await client.query(sig.follow_up)
                        async for message in client.receive_response():
                            _render_message(message, verbosity)
                            if isinstance(message, ResultMessage):
                                result_message = message
                    if verbosity == "verbose":
                        console.rule(style="dim")
                    _print_result_summary(result_message, verbosity)
                    console.print(f"\nTrace: [dim].slash/tracer.db[/dim]\n")

    except BaseException:
        # If the session crashed, mark the task abandoned so it doesn't surface
        # as a phantom open session on the next `slash repl` startup.
        if tracer and _task_session_id:
            try:
                row = tracer.conn.execute(
                    "SELECT status FROM tasks WHERE session_id = ?",
                    (_task_session_id,),
                ).fetchone()
                if row and row[0] == "open":
                    tracer.set_task_status(_task_session_id, "abandoned")
            except Exception:
                pass
        raise


def run_task(prompt: str, root: Path) -> None:
    """Entry point called from the CLI.

    Reads ``runtime.mode`` from the manifest.  When the mode is ``docker``,
    delegates execution to :class:`~slash.docker_runner.DockerRunner`; the
    agent runs inside an isolated container and this process simply wraps the
    ``docker run`` call.  In local mode the agent runs in-process as before.
    """
    manifest = _load_manifest(root)
    runtime  = manifest.get("runtime", {})

    # SLASH_FORCE_LOCAL is set by DockerRunner when it launches this process
    # inside a container.  It prevents re-entering docker mode when the
    # in-container slash reads the workspace manifest (which still says
    # mode: docker on the host side).
    import os
    force_local = os.environ.get("SLASH_FORCE_LOCAL") == "1"

    if not force_local and runtime.get("mode") == "docker":
        from slash.docker_runner import DockerRunner
        _print_runtime_banner(root)
        docker_config = runtime.get("docker", {})
        runner = DockerRunner(docker_config, root)
        verbosity = _load_verbosity_config(root)
        runner.run_run(prompt, verbosity=verbosity)
        return

    verbosity = _load_verbosity_config(root)
    anyio.run(_run_task_streaming, prompt, root, verbosity)
