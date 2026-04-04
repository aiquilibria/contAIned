#!/usr/bin/env python3
"""
Stop hook — runs QA checks when the agent signals it is done.

Which checks run is controlled by policy.qa.checks in manifest.yaml.
Each entry is either a bare exec-form array or a named object:

  - ["ruff", "check", "."]
  - name: tests
    command: ["pytest", "tests/", "-x", "-q"]
    when_changed: ["*.py"]   # skip if no matching files were touched

If checks is empty, QA passes trivially.
If any check fails → prints JSON with decision:block, agent receives feedback.
"""
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _policy import load_policy  # noqa: E402

try:
    event = json.load(sys.stdin)
except (json.JSONDecodeError, EOFError):
    event = {}

cwd      = event.get("cwd", ".")
TASK_DIR = Path(cwd).resolve()
policy   = load_policy(cwd)
checks   = policy["qa"].get("checks", [])

# ── Fetch touched files for when_changed evaluation ───────────────────────────
# None  → tracer lookup failed; when_changed guards are bypassed (all checks run).
# []    → no session or session has no recorded changes; when_changed checks skip.
_touched: list[str] | None = []
_session_id = event.get("session_id")
if _session_id:
    try:
        from contained.tracer import contAInedTracer  # noqa: PLC0415
        _tracer  = contAInedTracer(str(Path(cwd) / ".contAIned" / "tracer.db"))
        _touched = _tracer.list_touched_files(_session_id)
    except Exception:
        _touched = None  # tracer unavailable → bypass when_changed, run all checks


def run(cmd: list) -> tuple[int, str]:
    result = subprocess.run(cmd, cwd=TASK_DIR, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def _expand(entry) -> dict:
    """Normalise a bare array or named-object check entry into a uniform dict."""
    if isinstance(entry, list):
        return {"name": entry[0], "command": entry, "when_changed": []}
    return {
        "name":         entry.get("name", (entry.get("command") or ["?"])[0]),
        "command":      entry.get("command", []),
        "when_changed": entry.get("when_changed", []),
    }


def _files_match(touched: list[str] | None, patterns: list[str]) -> bool:
    """Return True if any touched filename matches any fnmatch glob pattern.

    Returns True when *touched* is None (tracer unavailable) so that
    when_changed guards are bypassed and all checks run unconditionally.
    """
    if touched is None:
        return True
    return any(
        fnmatch.fnmatch(Path(f).name, pat)
        for f in touched
        for pat in patterns
    )


check_results: list = []
failures: list = []


def record(name: str, status: str, output: str = "") -> None:
    check_results.append({"name": name, "status": status, "output": output})


# ── Run setup commands ────────────────────────────────────────────────────────
# Setup commands (from ecosystem install fields) run before any checks.
# A setup failure is immediately fatal — there is no point running checks
# if the workspace is not properly initialised.
for _setup_cmd in policy["qa"].get("setup", []):
    if not _setup_cmd:
        continue
    _setup_name = " ".join(_setup_cmd)
    try:
        _code, _out = run(_setup_cmd)
    except FileNotFoundError:
        record(_setup_name, "skip", f"{_setup_cmd[0]!r} not found")
        continue
    if _code != 0:
        record(_setup_name, "fail", _out)
        result: dict = {"checks": check_results}
        result["decision"] = "block"
        result["reason"] = f"QA setup failed — fix before finishing:\n\n### {_setup_name}\n```\n{_out}\n```\n"
        print(json.dumps(result))
        sys.exit(0)
    record(_setup_name, "pass")


# ── Run checks ────────────────────────────────────────────────────────────────
for _entry in checks:
    _check = _expand(_entry)
    _name  = _check["name"]
    _cmd   = _check["command"]
    _when  = _check["when_changed"]

    if not _cmd:
        record(_name, "skip", "no command specified")
        continue

    if _when and not _files_match(_touched, _when):
        record(_name, "skip", "no matching files touched")
        continue

    try:
        _code, _out = run(_cmd)
    except FileNotFoundError:
        record(_name, "skip", f"{_cmd[0]!r} not found")
        continue

    # Exit code 5 from pytest means no tests collected — treat as pass.
    if _code not in (0, 5):
        failures.append({"check": _name, "output": _out})
        record(_name, "fail", _out)
    else:
        record(_name, "pass")

# ── Emit result JSON (always) ─────────────────────────────────────────────────
result: dict = {"checks": check_results}
if failures:
    feedback = "QA failed — fix the following issues before finishing:\n\n"
    for item in failures:
        feedback += f"### {item['check']}\n```\n{item['output']}\n```\n\n"
    result["decision"] = "block"
    result["reason"] = feedback
print(json.dumps(result))
sys.exit(0)
