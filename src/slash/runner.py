"""
slash run — invoke the agent on a single task.

Validates that the workspace is initialised, then starts the Agent SDK
with the project settings (hooks + permissions) loaded from .claude/settings.json.
"""
import anyio
import yaml
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.text import Text

console = Console()


def _load_thinking_config(root: Path):
    """
    Read the ``agent.thinking`` block from ``.slash/policy/manifest.yaml`` and
    return a :class:`ThinkingConfigEnabled` instance when thinking is enabled,
    or ``None`` otherwise.
    """
    from claude_agent_sdk import ThinkingConfigEnabled

    manifest_path = root / ".slash" / "policy" / "manifest.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
    except FileNotFoundError:
        return None

    thinking_cfg = manifest.get("agent", {}).get("thinking", {})
    if thinking_cfg.get("enabled", False):
        budget = int(thinking_cfg.get("budget_tokens", 1024))
        return ThinkingConfigEnabled(type="enabled", budget_tokens=budget)
    return None


def _load_verbosity_config(root: Path) -> str:
    """
    Read the ``agent.verbosity`` value from ``.slash/policy/manifest.yaml``.

    Returns one of:
      - ``"verbose"``  — full streaming output (tool calls, results, thinking); **default**
      - ``"concise"``  — single updating status line showing the current tool call
      - ``"none"``     — no intermediate output; only the final result is printed
    """
    manifest_path = root / ".slash" / "policy" / "manifest.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
    except FileNotFoundError:
        return "verbose"

    value = manifest.get("agent", {}).get("verbosity", "verbose")
    if value not in ("verbose", "concise", "none"):
        return "verbose"
    return value


def _check_initialised(root: Path) -> list[str]:
    """Return a list of missing paths that indicate init has not been run."""
    required = [
        root / ".slash" / "hooks" / "restrict_writes.py",
        root / ".slash" / "hooks" / "audit.py",
        root / ".slash" / "hooks" / "qa.py",
        root / ".slash" / "policy" / "manifest.yaml",
        root / ".claude" / "settings.json",
    ]
    return [str(p.relative_to(root)) for p in required if not p.exists()]


def _tool_input_summary(name: str, input: dict) -> str:
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


async def _can_use_tool(name: str, input_data: dict, context: object) -> object:
    """
    Permission callback wired into every agent run.

    ``AskUserQuestion`` is intercepted here: the question is printed to the
    console, the runner pauses and reads the user's answer from stdin, then
    returns a ``PermissionResultDeny`` whose message carries the answer.
    The agent receives *"User answered: <answer>"* as the tool result and
    continues — it never sees a confusing error because it asked a question.

    All other tools are passed through unconditionally; the existing hook
    files and ``settings.json`` permissions handle the real policy enforcement.
    """
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    if name == "AskUserQuestion":
        # AskUserQuestion uses a ``questions`` array where each entry has
        # ``question`` (text), ``header``, ``multiSelect``, and ``options``
        # (list of {label, description} objects).  We render each question
        # in sequence, collect answers, and return them all so the agent has
        # full context.
        questions = input_data.get("questions", [])

        # Graceful fallback: legacy / simplified callers that pass a bare
        # ``question`` / ``prompt`` string with a flat ``options`` list.
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

            # Read user input on a thread so we don't block the anyio event loop.
            answer = await anyio.to_thread.run_sync(
                lambda: input("  Your answer: ").strip()
            )
            console.print()
            collected.append(f"Q: {text}\nA: {answer}")

        answers_text = "\n\n".join(collected)
        return PermissionResultDeny(
            behavior="deny",
            message=f"User answered:\n{answers_text}",
            interrupt=False,
        )

    return PermissionResultAllow(behavior="allow")


def _build_client(root: Path):
    """
    Validate the workspace and return a ``ClaudeSDKClient`` instance.

    The returned client is intended to be used as an async context manager::

        async with _build_client(root) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                ...

    Raises ``SystemExit(1)`` if the workspace has not been initialised.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    missing = _check_initialised(root)
    if missing:
        console.print("\n[red]Error:[/red] workspace not initialised. Run [bold]slash init[/bold] first.\n")
        console.print("Missing:")
        for m in missing:
            console.print(f"  [dim]{m}[/dim]")
        console.print()
        raise SystemExit(1)

    options = ClaudeAgentOptions(
        setting_sources=["project"],
        allowed_tools=[
            "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Bash",
            "AskUserQuestion",
        ],
        cwd=str(root),

        # Extended thinking — config sourced from .slash/policy/manifest.yaml
        thinking=_load_thinking_config(root),

        # Intercept AskUserQuestion to prompt the user and feed the answer back.
        can_use_tool=_can_use_tool,
    )

    return ClaudeSDKClient(options)


def _render_message(message: object, verbosity: str, live=None) -> None:
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
                    console.print(escape(block.text))

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

    token_parts: list[str] = []
    if input_tokens is not None:
        token_parts.append(f"in {input_tokens:,}")
    if output_tokens is not None:
        token_parts.append(f"out {output_tokens:,}")
    if input_tokens is None and output_tokens is None and total_tokens is not None:
        token_parts.append(f"total {total_tokens:,}")
    if cache_read:
        token_parts.append(f"cache-read {cache_read:,}")
    if cache_create:
        token_parts.append(f"cache-write {cache_create:,}")
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
        console.print("  " + "  ·  ".join(cost_parts))


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

    result_message = None

    async with _build_client(root) as client:
        await client.query(prompt)

        # ── verbose ───────────────────────────────────────────────────────────
        if verbosity == "verbose":
            async for message in client.receive_response():
                _render_message(message, "verbose")
                if isinstance(message, ResultMessage):
                    result_message = message

        # ── concise ───────────────────────────────────────────────────────────
        elif verbosity == "concise":
            initial = Text.from_markup("  [dim]Starting…[/dim]")
            with Live(initial, console=console, refresh_per_second=10) as live:
                async for message in client.receive_response():
                    _render_message(message, "concise", live=live)
                    if isinstance(message, ResultMessage):
                        result_message = message

        # ── none ──────────────────────────────────────────────────────────────
        else:
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    result_message = message

    # ── Footer ────────────────────────────────────────────────────────────────
    if verbosity == "verbose":
        console.rule(style="dim")

    _print_result_summary(result_message, verbosity)

    console.print(f"\nAudit log: [dim].slash/audit/pipeline.jsonl[/dim]\n")


def run_task(prompt: str, root: Path) -> None:
    """Entry point called from the CLI."""
    verbosity = _load_verbosity_config(root)
    anyio.run(_run_task_streaming, prompt, root, verbosity)
