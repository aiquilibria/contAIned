#!/usr/bin/env python3
"""
contAIned status line — four-segment display.

Baked into the container image at /etc/contained/statusline.py and wired
into Claude Code via the statusLine setting in managed-settings.json.
Claude Code pipes a JSON object on stdin on every update; this script
reads it and prints a single formatted line to stdout.

Segments:
  1. [✦]
  2. <branch (shortened)> <commit_hash (7 chars)>
  3. <session_id (8 chars)> ctx <pct>% <cost>
  4. <work_unit_id (8 chars)> cont[AI✦]ned v<version>
"""

import json
import subprocess
import sys

# ── ANSI helpers ─────────────────────────────────────────────────────────────
_RESET = "\033[0m"
_GREEN = "\033[32m"
_RED   = "\033[31m"

_FG_BLACK = "\033[30m"
_FG_WHITE = "\033[97m"

_BG_WHITE  = "\033[107m"
_BG_GREEN  = "\033[42m"
_BG_ORANGE = "\033[43m"  # standard yellow — renders as amber/orange
_BG_RED    = "\033[41m"


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


def _shorten(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    cwd        = data.get("cwd") or ""
    session_id = data.get("session_id") or ""

    # ── Segment 1: [✦] ────────────────────────────────────────────────────────
    seg1 = f"{_GREEN}[{_RESET}{_RED}✦{_RESET}{_GREEN}]{_RESET}"

    # ── Segment 2: branch commit_hash +ins -del ───────────────────────────────
    seg2 = ""
    if cwd and _git(cwd, "rev-parse", "--git-dir"):
        branch = _git(cwd, "branch", "--show-current") or _git(cwd, "rev-parse", "--short", "HEAD")
        commit = _git(cwd, "rev-parse", "--short", "HEAD")
        branch = _shorten(branch, 20)
        seg2 = f"{_BG_WHITE}{_FG_BLACK} ⎇ {branch} {commit} {_RESET}"

        shortstat = _git(cwd, "diff", "--shortstat", "HEAD")
        ins = del_ = 0
        for token in shortstat.split(","):
            token = token.strip()
            if "insertion" in token:
                ins = int(token.split()[0])
            elif "deletion" in token:
                del_ = int(token.split()[0])
        if ins or del_:
            seg2 += f"  {_GREEN}+{ins}{_RESET} {_RED}-{del_}{_RESET}"

    # ── Segment 3: session_id ctx pct cost ────────────────────────────────────
    parts3: list[str] = []

    if session_id:
        parts3.append(session_id[:8])

    ctx: float = (data.get("context_window") or {}).get("used_percentage") or 0
    if ctx <= 50:
        bg, fg = _BG_GREEN, _FG_BLACK
    elif ctx <= 80:
        bg, fg = _BG_ORANGE, _FG_BLACK
    else:
        bg, fg = _BG_RED, _FG_WHITE
    parts3.append(f"{bg}{fg} ctx {ctx:.0f}% {_RESET}")

    cost = (data.get("cost") or {}).get("total_cost_usd")
    if cost is not None:
        parts3.append(f"${cost:.4f}")

    seg3 = " ".join(parts3)

    # ── Segment 4: work_unit_id cont[AI✦]ned vX.Y.Z ──────────────────────────
    parts4: list[str] = []

    if session_id and cwd:
        try:
            from pathlib import Path

            from contained.tracer import contAInedTracer  # noqa: PLC0415
            db_path = str(Path(cwd) / ".contAIned" / "tracer.db")
            tracer = contAInedTracer(db_path)
            wu_id = tracer.get_active_work_unit(session_id)
            if wu_id:
                parts4.append(wu_id[:8])
        except Exception:
            pass

    ver = ""
    try:
        ver = open("/etc/contained/version").read().strip().lstrip("v")
    except Exception:
        pass
    if not ver:
        try:
            from importlib.metadata import version as _pkg_version
            ver = _pkg_version("contained").lstrip("v")
        except Exception:
            pass

    brand = f"{_GREEN}cont[{_RESET}{_RED}AI✦{_RESET}{_GREEN}]ned{_RESET}"
    if ver:
        brand += f" v{ver}"
    parts4.append(brand)

    seg4 = " ".join(parts4)

    segments = [s for s in (seg1, seg2, seg3, seg4) if s]
    if segments:
        print(" │ ".join(segments))


if __name__ == "__main__":
    main()
