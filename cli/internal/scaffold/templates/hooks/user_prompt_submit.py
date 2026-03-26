#!/usr/bin/env python3
"""
UserPromptSubmit hook — registers the root session in tracer.db on the first prompt,
opens or finds the current work unit, and records a policy snapshot.

Also audits operator shell escapes (! commands) if the SDK delivers them here.

Claude Code docs state this hook fires "when the user submits a prompt, before Claude
processes it" with no documented exception for ! shell escapes.  Whether ! commands
actually reach this hook is unconfirmed; the detection below is conditional — it logs
only when the prompt starts with "!" so legitimate prompts are unaffected either way.

Session history, audit logs, and file diffs are queryable via the /contained:tracer
skill backed by the tracer MCP server.
"""
import json
import subprocess
import sys
from pathlib import Path

try:
    event = json.load(sys.stdin)
except (json.JSONDecodeError, EOFError):
    sys.exit(0)

prompt     = (event.get("prompt") or "").strip()
cwd        = event.get("cwd", ".")
session_id = event.get("session_id")
agent_id   = event.get("agent_id")

if session_id and not agent_id:
    db_path = Path(cwd) / ".contAIned" / "tracer.db"
    if db_path.exists():
        try:
            from contained.tracer import contAInedTracer  # noqa: PLC0415
            tracer = contAInedTracer(str(db_path))

            # ── Register root session on first prompt (idempotent) ────────────
            tracer.open_task(session_id, prompt)

            # ── Work unit registration ────────────────────────────────────────
            # Detect git state and associate this session with a work unit.
            # Errors are silently swallowed so git absence never blocks the agent.
            try:
                _git_url = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    capture_output=True, text=True, cwd=cwd, timeout=5,
                ).stdout.strip()
                _git_branch = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, cwd=cwd, timeout=5,
                ).stdout.strip()
                _git_base = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True, cwd=cwd, timeout=5,
                ).stdout.strip()
                if _git_url and _git_branch and _git_base:
                    _work_unit_id = tracer.open_or_find_work_unit(
                        repo_url=_git_url,
                        base_branch=_git_branch,
                        base_commit=_git_base,
                        prompt=prompt or "(no prompt)",
                    )
                    tracer.register_session_in_work_unit(_work_unit_id, session_id)
                    tracer.record_policy_snapshot(
                        work_unit_id=_work_unit_id,
                        session_id=session_id,
                        manifest_path=str(Path(cwd) / ".contAIned" / "manifest.yaml"),
                        provenance_path="/run/contained/provenance.yaml",
                    )
            except Exception:
                pass

            # ── Audit operator shell escapes (! commands) ─────────────────────
            # If the SDK delivers ! commands to this hook, prompt starts with "!".
            # Log them as OperatorShell events so they appear in the audit trail
            # alongside agent tool calls.  If the SDK intercepts ! before firing
            # this hook, this block never runs — no false positives either way.
            if prompt.startswith("!"):
                tracer.log_event(
                    session_id=session_id,
                    tool="OperatorShell",
                    tool_input={"command": prompt[1:].strip()},
                    outcome="operator",
                    reason="operator shell escape (!)",
                )
        except Exception:
            pass

sys.exit(0)
