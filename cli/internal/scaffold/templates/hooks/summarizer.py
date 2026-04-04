#!/usr/bin/env python3
"""
Stop hook — runs QA checks, processes any pending git push, and closes the task.

Fires only for root-agent Stop events (not SubagentStop — that is wired to
subagent_stop.py).  This is the sole Stop hook; it owns QA, push processing,
and task closure.

Flow:
  1. Sentinel check: if task already "closed", exit 0 (no-op).
  2. Run qa.py inline — if any check fails, block and return to agent immediately.
  3. Defensive child check: poll up to 3 × 200 ms for open sub-agent sessions.
  4. Build provenance_log — append current container provenance to any existing
     entries so resumed tasks preserve the full signing history across rebuilds.
  5. Collect file diffs and action log for the operator summary UI.
  6. If a git push happened this session: build_actions, assemble_proof, POST,
     mark work unit pushed, open next work unit.
  7. Store transcript_path; set task status = closed.
  8. Block with a formatted summary so Claude surfaces it to the operator.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    event = json.load(sys.stdin)
except (json.JSONDecodeError, EOFError):
    event = {}

cwd        = event.get("cwd", ".")
session_id = event.get("session_id")

# A Stop event for a sub-agent carries agent_id; the summarizer only runs for
# the root session.  This guard is a safety net — the hook is registered for
# Stop only, not SubagentStop, so agent_id should never be present here.
if event.get("agent_id"):
    sys.exit(0)

if not session_id:
    sys.exit(0)

# ── File sentinel: prevents infinite loop on the second Stop ──────────────────
# Written just before the first block; on the second Stop the agent stops
# cleanly without rebuilding the summary.  File-based so it works even when
# the task row does not exist in the tracer DB (e.g. unregistered sessions) —
# the DB sentinel below is unreliable in that case because set_task_status is
# a bare UPDATE that silently no-ops when no row exists, so status never
# becomes "closed" and the DB sentinel never fires.
# NOTE: the sentinel is NOT checked here — it is evaluated after the tracer is
# imported so we can verify there is no unprocessed push before exiting early.
_sentinel_file = Path("/tmp/claude") / f".stop_done_{session_id[:16]}"

# ── Phase 1: QA checks ────────────────────────────────────────────────────────
# Run qa.py inline before building the summary or showing the approval UI.
# This guarantees QA always completes first regardless of whether the SDK
# executes Stop hooks sequentially or in parallel.
_qa_checks: list = []
_qa_script = Path(cwd) / ".contAIned" / "hooks" / "qa.py"
if _qa_script.exists():
    try:
        _qa_proc = subprocess.run(
            [sys.executable, str(_qa_script)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        if _qa_proc.stdout.strip():
            try:
                _qa_out = json.loads(_qa_proc.stdout.strip())
                if _qa_out.get("decision") == "block":
                    # QA failed — relay only the block decision to the SDK.
                    print(json.dumps({
                        "decision": "block",
                        "reason": _qa_out.get("reason", "QA checks failed"),
                    }))
                    sys.exit(0)
                _qa_checks = _qa_out.get("checks", [])
            except json.JSONDecodeError:
                _qa_checks = []
    except Exception:
        _qa_checks = []
        pass  # qa unavailable — proceed to approval UI

db_path = str(Path(cwd) / ".contAIned" / "tracer.db")

# ── Import tracer ──────────────────────────────────────────────────────────────
try:
    from contained.tracer import contAInedTracer  # noqa: PLC0415
    tracer = contAInedTracer(db_path)
except Exception:
    # If the tracer is unavailable, fall back to the file sentinel and stop.
    if _sentinel_file.exists():
        sys.exit(0)
    sys.exit(0)

# ── Check for an unprocessed push ─────────────────────────────────────────────
# Both sentinels below must yield to a pending push: if the session tree has a
# successful GitPush event and the work unit is still open, we must not skip
# the push-processing section — even if a previous Stop already ran.
_has_push_to_process = False
try:
    if tracer.get_active_work_unit(session_id):
        _sentinel_tree = tracer.tree_session_ids(session_id) or [session_id]
        _sentinel_ph = ",".join("?" * len(_sentinel_tree))
        _has_push_to_process = bool(
            tracer.conn.execute(
                f"SELECT 1 FROM audit_events WHERE session_id IN ({_sentinel_ph})"
                " AND tool = 'GitPush' AND outcome = 'success' LIMIT 1",
                _sentinel_tree,
            ).fetchone()
        )
except Exception:
    pass

# ── File sentinel ──────────────────────────────────────────────────────────────
if _sentinel_file.exists() and not _has_push_to_process:
    sys.exit(0)

# ── DB sentinel: second Stop after Claude has already presented the summary ───
# The first pass stores the summary and blocks with it for Claude to format.
# On the second Stop (after Claude has presented and stopped again), the task
# is already "closed" — exit 0 so the agent stops cleanly.
try:
    _status_row = tracer.conn.execute(
        "SELECT status FROM tasks WHERE session_id = ?", (session_id,)
    ).fetchone()
    if _status_row and _status_row[0] == "closed" and not _has_push_to_process:
        sys.exit(0)
except Exception:
    pass

# ── Defensive child check ──────────────────────────────────────────────────────
# The SDK fires SubagentStop for all children before the root Stop, but under
# parallel sub-agent execution there may be a brief race.  Poll up to 3 times.
open_children = []
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

# ── Build provenance log ───────────────────────────────────────────────────────
# Read the provenance snapshot bind-mounted read-only at container startup
# (see docker_runner.py).  Merge with any existing log entries so that tasks
# resumed after a container rebuild accumulate the full signing history —
# each closure records exactly which signed image was running at that point.
_prov_log: list = []
try:
    _existing_row = tracer.conn.execute(
        "SELECT summary FROM tasks WHERE session_id = ?", (session_id,)
    ).fetchone()
    if _existing_row and _existing_row[0]:
        _prov_log = json.loads(_existing_row[0]).get("provenance_log", [])
except Exception:
    pass

_prov_snapshot = Path("/run/contained/provenance.yaml")
if _prov_snapshot.exists():
    try:
        import yaml as _yaml
        _prov = _yaml.safe_load(_prov_snapshot.read_text()) or {}
        _prov_log.append({
            "closed_at":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "image_digest":      _prov.get("image_digest", ""),
            "operator_identity": _prov.get("operator_identity", ""),
            "oidc_issuer":       _prov.get("oidc_issuer", ""),
            "signed_at":         _prov.get("signed_at", ""),
            "rekor_log_index":   _prov.get("rekor_log_index"),
            "rekor_entry_url":   _prov.get("rekor_entry_url", ""),
        })
    except Exception:
        pass

# ── Collect file diffs ─────────────────────────────────────────────────────────
try:
    touched_files = tracer.list_touched_files(session_id)
except Exception:
    touched_files = []

# ── Skip summary UI if nothing was written this session ───────────────────────
if not touched_files:
    try:
        tracer.set_task_status(session_id, "closed", summary={"provenance_log": _prov_log, "file_changes": [], "action_log": [], "qa_checks": _qa_checks})
    except Exception:
        pass
    sys.exit(0)

# Build a lookup of write-tool audit events per file path (for reasons).
try:
    all_audit = tracer.recent_audit_events(session_id, limit=500)
    _write_events_by_file: dict = {}
    for _ev in reversed(all_audit):
        if _ev["tool"] in ("Write", "Edit", "MultiEdit") and _ev.get("input"):
            _fp = _ev["input"].get("file_path") or ""
            if _fp:
                _write_events_by_file.setdefault(_fp, []).append(_ev)
            # MultiEdit may touch multiple files
            for _mfp in (_ev["input"].get("file_paths") or []):
                _write_events_by_file.setdefault(_mfp, []).append(_ev)
except Exception:
    _write_events_by_file = {}

# Resolve session tree once for baseline lookups.
try:
    _tree_ids = tracer.tree_session_ids(session_id)
    _tree_placeholders = ",".join("?" * len(_tree_ids)) if _tree_ids else "'__none__'"
except Exception:
    _tree_ids = []
    _tree_placeholders = "'__none__'"

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
            if _tree_ids:
                _bl = tracer.conn.execute(
                    f"SELECT pre_hash FROM baselines WHERE file_path = ? AND session_id IN ({_tree_placeholders}) ORDER BY captured_at ASC LIMIT 1",
                    [file_path, *_tree_ids],
                ).fetchone()
                if _bl is not None:
                    change_type = "new file" if _bl[0] is None else "modified"
        except Exception:
            pass

        # Build a short human-readable reason from write-tool events for this file.
        _file_write_evs = _write_events_by_file.get(file_path, [])
        if _file_write_evs:
            _tools_used = list(dict.fromkeys(e["tool"] for e in _file_write_evs))
            reason = ", ".join(t.lower() for t in _tools_used)
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

# ── Build action log ───────────────────────────────────────────────────────────
try:
    raw_events = tracer.recent_audit_events(session_id, limit=200)
    action_log = [
        e for e in reversed(raw_events)
        if e["tool"] in ("Bash", "Agent") or e["outcome"] == "denied"
    ]
except Exception:
    action_log = []

# ── Look up task prompt ────────────────────────────────────────────────────────
try:
    row = tracer.conn.execute(
        "SELECT prompt, started_at FROM tasks WHERE session_id = ?", (session_id,)
    ).fetchone()
    task_prompt    = row[0] if row else "(unknown)"
    task_started   = row[1] if row else None
except Exception:
    task_prompt  = "(unknown)"
    task_started = None

# ── Assemble and store summary ─────────────────────────────────────────────────
summary = {
    "provenance_log": _prov_log,
    "file_changes": [
        {
            "file_path":   d["file_path"],
            "lines_added": d["lines_added"],
            "lines_removed": d["lines_removed"],
            "change_type": d["change_type"],
            "reason":      d["reason"],
        }
        for d in file_diffs
    ],
    "action_log": action_log,
    "incomplete_children": open_children,
    "qa_checks": _qa_checks,
}

# ── Locate Claude Code transcript for this session ────────────────────────────
# Claude Code injects transcript_path directly into the hook event payload.
# Do NOT reconstruct this path from cwd: inside the container cwd resolves to
# /workspace, but Claude Code names the project directory using the host
# absolute path, so a reconstructed path would never match the real file.
_transcript_path = event.get("transcript_path", "") or ""

# ── Enrich action_log Bash entries with full stdout from transcript ────────────
# The transcript holds untruncated tool outputs; the audit_events table has only
# the first 500 chars.  We merge in-memory so the stored summary is richer.
if _transcript_path and Path(_transcript_path).exists():
    try:
        from contained.tracer import extract_tool_outputs_from_transcript  # noqa: PLC0415
        _tool_outputs = extract_tool_outputs_from_transcript(_transcript_path)
        # Build per-command output lists (same command may be run multiple times).
        _bash_outputs: dict = {}
        for _to in _tool_outputs:
            if _to["tool_name"] == "Bash":
                _cmd = (_to["input"].get("command") or "").strip()
                if _cmd:
                    _bash_outputs.setdefault(_cmd, []).append({
                        "output":    _to["output"],
                        "exit_code": _to["exit_code"],
                    })
        # Enrich action_log entries in-place (mutates the in-memory list only).
        _bash_use_counts: dict = {}
        for _entry in action_log:
            if _entry.get("tool") == "Bash":
                _cmd = ((_entry.get("input") or {}).get("command") or "").strip()
                _idx = _bash_use_counts.get(_cmd, 0)
                _bash_use_counts[_cmd] = _idx + 1
                _avail = _bash_outputs.get(_cmd, [])
                if _idx < len(_avail):
                    _full = _avail[_idx]
                    _entry.setdefault("input", {})
                    _entry["input"]["stdout"] = _full["output"][:10240]  # cap at 10 KB
                    if _full["exit_code"] is not None:
                        _entry["input"]["exit_code"] = _full["exit_code"]
    except Exception:
        pass

# Rebuild summary with enriched action_log.
summary["action_log"] = action_log

# ── Process any pending git push ───────────────────────────────────────────────
# Detect a successful git push this session; if found, build the ATP work unit
# payload, POST it to mAInlined, close the work unit, and open the next one.
_push_found = False
_submitted = False
_submit_error: str | None = None
try:
    _wu_id = tracer.get_active_work_unit(session_id)
    if _wu_id:
        # Use GitPush audit events logged by push_hook.py — already filtered by
        # regex and only logged on successful pushes, so no exit_code check needed.
        # Search the full session tree so sub-agent pushes are detected too.
        _tree_session_ids = _tree_ids or [session_id]
        _ph = ",".join("?" * len(_tree_session_ids))
        _push_found = bool(
            tracer.conn.execute(
                f"SELECT 1 FROM audit_events WHERE session_id IN ({_ph}) AND tool = 'GitPush' AND outcome = 'success' LIMIT 1",
                _tree_session_ids,
            ).fetchone()
        )

        if _push_found:
            _head_res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, cwd=cwd, timeout=10,
            )
            _head_commit = _head_res.stdout.strip() if _head_res.returncode == 0 else ""
            _head_branch_res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, cwd=cwd, timeout=10,
            )
            _head_branch = _head_branch_res.stdout.strip() if _head_branch_res.returncode == 0 else None

            if _qa_checks:
                _qa_passed = all(
                    c.get("status") == "pass"
                    for c in _qa_checks
                    if c.get("status") != "skip"
                )
                tracer.record_qa_result(_wu_id, {"checks": _qa_checks, "passed": _qa_passed})

            _wu_narrative = None
            if _transcript_path and Path(_transcript_path).exists():
                try:
                    from contained.tracer import extract_session_narrative  # noqa: PLC0415
                    _narrative_dict = extract_session_narrative(_transcript_path)
                    if _narrative_dict:
                        _wu_narrative = json.dumps(_narrative_dict, ensure_ascii=False)
                except Exception:
                    pass
            if _wu_narrative:
                try:
                    tracer.record_narrative(_wu_id, _wu_narrative)
                except Exception:
                    pass

            tracer.build_actions(_wu_id, [t for t in [_transcript_path] if t])

            try:
                _payload = tracer.assemble_proof(_wu_id)
            except Exception:
                _payload = None

            if _payload:
                try:
                    _mAInlined_url = None
                    _manifest_path = Path(cwd) / ".contAIned" / "manifest.yaml"
                    if _manifest_path.exists():
                        import yaml as _yaml  # noqa: PLC0415
                        from urllib.parse import urlparse, urlunparse  # noqa: PLC0415
                        _manifest = _yaml.safe_load(_manifest_path.read_text()) or {}
                        # v2 schema: init.mainlined; v1 fallback: root mainlined
                        _mainlined_sec = (
                            _manifest.get("init", {}).get("mainlined", {})
                            or _manifest.get("mainlined", {})
                        )
                        _bootstrap_url = _mainlined_sec.get("url", "")
                        # Prefer the in-container URL from policy_yaml (Docker network
                        # alias, e.g. "http://mainlined:8080") over mainlined.url which
                        # is the host-side bootstrap URL and may point to localhost.
                        # Graft the path from mainlined.url so the full submission
                        # endpoint is preserved (e.g. /aiquilibria/default).
                        _policy_base_url = ""
                        _policy_yaml_str = _mainlined_sec.get("policy_yaml", "")
                        if _policy_yaml_str:
                            try:
                                _policy_doc = _yaml.safe_load(_policy_yaml_str) or {}
                                # v2 schema: init.mainlined.url; v1 fallback: policy.mAInlined.url
                                _policy_base_url = (
                                    _policy_doc.get("init", {}).get("mainlined", {}).get("url", "")
                                    or _policy_doc.get("policy", {}).get("mAInlined", {}).get("url", "")
                                )
                            except Exception:
                                pass
                        if _policy_base_url and _bootstrap_url:
                            _pb = urlparse(_policy_base_url)
                            _pf = urlparse(_bootstrap_url)
                            _mAInlined_url = urlunparse((
                                _pb.scheme, _pb.netloc,
                                _pf.path, _pf.params, _pf.query, _pf.fragment,
                            ))
                        else:
                            _mAInlined_url = _policy_base_url or _bootstrap_url
                    if _mAInlined_url:
                        _mAInlined_url = _mAInlined_url.rstrip("/") + "/proof/submit"
                    _secret_key_path = Path("/run/contained/secrets/mainlined_api_key")
                    _mAInlined_key = _secret_key_path.read_text().strip() if _secret_key_path.exists() else None
                    if _mAInlined_url and _mAInlined_key:
                        import urllib.request  # noqa: PLC0415
                        import urllib.error    # noqa: PLC0415
                        _req = urllib.request.Request(
                            _mAInlined_url,
                            data=json.dumps(_payload).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {_mAInlined_key}",
                            },
                            method="POST",
                        )
                        try:
                            with urllib.request.urlopen(_req, timeout=30) as _resp:
                                _resp_status = _resp.status
                            if 200 <= _resp_status < 300:
                                _submitted = True
                            else:
                                _submit_error = f"HTTP {_resp_status}"
                        except urllib.error.HTTPError as _he:
                            _submit_error = f"HTTP {_he.code}: {_he.read().decode('utf-8', errors='replace')[:200]}"
                        except Exception as _se:
                            _submit_error = str(_se)
                    elif not _mAInlined_url:
                        _submit_error = "could not resolve mAInlined URL from manifest"
                    elif not _mAInlined_key:
                        _submit_error = "mainlined_api_key not found"
                except Exception as _pe:
                    _submit_error = f"payload/URL error: {_pe}"

            if _head_commit and _submitted:
                tracer.complete_work_unit(_wu_id, _head_commit, head_branch=_head_branch)
                try:
                    _wu_row = tracer.conn.execute(
                        "SELECT repo_url FROM work_units WHERE id = ?",
                        (_wu_id,),
                    ).fetchone()
                    if _wu_row:
                        tracer.open_or_find_work_unit(
                            repo_url=_wu_row[0],
                            base_branch=_head_branch or "",
                            base_commit=_head_commit,
                            prompt="(continued after push)",
                        )
                except Exception:
                    pass
except Exception:
    pass

# ── Store transcript path and close task ──────────────────────────────────────
if _transcript_path:
    try:
        tracer.set_task_transcript_path(session_id, _transcript_path)
    except Exception:
        pass

try:
    tracer.set_task_status(session_id, "closed", summary=summary)
except Exception:
    pass

# Block with a formatted summary so Claude surfaces it to the operator.
# Touch the file sentinel before blocking so the next Stop exits 0 cleanly
# regardless of whether the DB task row was created or not.
_status_icons = {"pass": "✓", "fail": "✗", "skip": "·"}
_qa_line = "  ".join(
    f"{_status_icons.get(c['status'], '?')} {c['name']}"
    for c in _qa_checks
) if _qa_checks else "(no checks recorded)"

_changed = summary.get("file_changes", [])
_files_line = "\n".join(
    f"- {c['file_path'].replace('/workspace/', '')} (+{c['lines_added']}/-{c['lines_removed']})"
    for c in _changed
) if _changed else "- (no file changes)"

_prov_line = ""
if _prov_log:
    _latest = _prov_log[-1]
    _digest_short = (_latest.get("image_digest") or "")[:19]
    _operator = _latest.get("operator_identity") or ""
    _rekor = _latest.get("rekor_log_index")
    _prov_line = f"\nProvenance: {_digest_short}… · {_operator}" + (f" · Rekor #{_rekor}" if _rekor else "")

_proof_line = ""
if _push_found:
    if _submitted:
        _proof_line = "\nProof: submitted ✓"
    elif _submit_error:
        _proof_line = f"\nProof: FAILED — {_submit_error}"

_summary_msg = f"QA: {_qa_line}\n\nChanged:\n{_files_line}{_prov_line}{_proof_line}\n"
try:
    _sentinel_file.touch()
except Exception:
    pass
print(json.dumps({"decision": "block", "reason": _summary_msg}))
sys.exit(0)
