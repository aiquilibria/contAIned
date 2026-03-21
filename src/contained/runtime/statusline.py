#!/usr/bin/env python3
"""
contAIned status line — git branch, diff stat, session cost, context %.

Baked into the container image at /etc/contained/statusline.py and wired
into Claude Code via the statusLine setting in managed-settings.json.
Claude Code pipes a JSON object on stdin on every update; this script
reads it and prints a single formatted line to stdout.
"""

import json
import subprocess
import sys

# ── ANSI helpers ─────────────────────────────────────────────────────────────
_RESET = "\033[0m"
_GREEN = "\033[32m"
_RED = "\033[31m"

# Foreground colours
_FG_BLACK = "\033[30m"
_FG_WHITE = "\033[97m"

# Background colours
_BG_WHITE = "\033[107m"
_BG_GREEN = "\033[42m"
_BG_ORANGE = "\033[43m"  # standard yellow — renders as amber/orange
_BG_RED = "\033[41m"


def _git(cwd: str, *args: str) -> str:
    """Run a git command in *cwd*; return stdout or empty string on error."""
    try:
        return subprocess.check_output(
            ["git", "-C", cwd, *args],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    contained_part = f"{_GREEN} [{_RESET}{_RED}✦{_RESET}{_GREEN}]{_RESET}"

    cwd = data.get("cwd") or ""

    # ── Git branch + uncommitted diff stat ───────────────────────────────────
    git_part = ""
    if cwd and _git(cwd, "rev-parse", "--git-dir"):
        branch = _git(cwd, "branch", "--show-current") or _git(cwd, "rev-parse", "--short", "HEAD")
        shortstat = _git(cwd, "diff", "--shortstat", "HEAD")
        ins = del_ = 0
        for token in shortstat.split(","):
            token = token.strip()
            if "insertion" in token:
                ins = int(token.split()[0])
            elif "deletion" in token:
                del_ = int(token.split()[0])

        # Branch badge: nerd-font branch icon + name on white bg / black fg
        if len(branch) > 24:
            branch = f"{branch[:21]}..."
        branch_badge = f"{_BG_WHITE}{_FG_BLACK} ⎇ {branch} {_RESET}"
        git_part = branch_badge

        if ins or del_:
            ins_str = f"{_GREEN}+{ins}{_RESET}"
            del_str = f"{_RED}-{del_}{_RESET}"
            git_part += f"  {ins_str} {del_str}"

    # ── Session cost ─────────────────────────────────────────────────────────
    cost_part = ""
    cost = (data.get("cost") or {}).get("total_cost_usd")
    if cost is not None:
        cost_part = f"${cost:.4f}"

    # ── Context window usage ─────────────────────────────────────────────────
    ctx_part = ""
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        if ctx <= 50:
            bg, fg = _BG_GREEN, _FG_BLACK
        elif ctx <= 80:
            bg, fg = _BG_ORANGE, _FG_BLACK
        else:
            bg, fg = _BG_RED, _FG_WHITE
        ctx_part = f"{bg}{fg} ctx {ctx}% {_RESET}"

    # ── Session ID ────────────────────────────────────────────────────────────
    session_part = ""
    session_id = data.get("session_id") or ""
    if session_id:
        session_part = session_id[:8]

    parts = [p for p in (contained_part, git_part, cost_part, ctx_part, session_part) if p]
    if parts:
        print(" │ ".join(parts))


if __name__ == "__main__":
    main()
