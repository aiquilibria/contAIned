"""
contAInedTracer — continuous write tracking and task review.

Implements the core DB and write-capture API described in docs/contAIned-trace.md.

Phase 1: contAInedTracer class with full schema, blob store, baselines, snapshots,
audit events, task lifecycle, tree diffing, and GC.

Phase 2: Work unit tracking, policy snapshots, actions timeline, and payload
assembly for ATP proof submission at git push time.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import threading as _threading
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- Content-addressed blob store.
-- Identical content is stored once regardless of how many agents wrote it.
CREATE TABLE IF NOT EXISTS blobs (
    hash        TEXT    PRIMARY KEY,
    content     BLOB    NOT NULL,       -- raw file content, zlib-compressed
    size_bytes  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL        -- Unix timestamp (ms)
);

-- Task / session registry.
-- One row per contAIned run invocation, REPL session, or sub-agent session.
CREATE TABLE IF NOT EXISTS tasks (
    session_id          TEXT    PRIMARY KEY,
    parent_session_id   TEXT    REFERENCES tasks(session_id),
    prompt              TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'open',  -- open|closed
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    summary             TEXT,   -- JSON: per-file diff summary, populated at close
    transcript_path     TEXT    -- absolute path to the Claude Code JSONL transcript
);

CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- Per-session file baseline.
-- Captured by tracer_pre.py before the first Write/Edit/MultiEdit to a file.
-- NULL pre_hash means the file did not exist before this session (new file).
CREATE TABLE IF NOT EXISTS baselines (
    session_id  TEXT    NOT NULL REFERENCES tasks(session_id),
    file_path   TEXT    NOT NULL,
    pre_hash    TEXT    REFERENCES blobs(hash),
    captured_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, file_path)
);

-- Append-only write event log.
-- One row per successful Write/Edit/MultiEdit, in order.
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL REFERENCES tasks(session_id),
    file_path   TEXT    NOT NULL,
    blob_hash   TEXT    NOT NULL REFERENCES blobs(hash),
    written_at  INTEGER NOT NULL,   -- Unix timestamp (ms)
    metadata    TEXT,               -- Optional JSON: pass number, notes, etc.
    diff_hash   TEXT    REFERENCES blobs(hash)  -- unified diff, NULL for new/binary
);

CREATE INDEX IF NOT EXISTS idx_snapshots_file
    ON snapshots(file_path, written_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_session
    ON snapshots(session_id, written_at DESC);

-- Audit event log (replaces .contAIned/audit/pipeline.jsonl).
-- One row per tool call (all tools, not just writes).
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,   -- ISO-8601 timestamp
    session_id  TEXT    REFERENCES tasks(session_id),
    tool        TEXT    NOT NULL,
    input       TEXT,               -- JSON: tool-specific trace unit (see log_event)
    outcome             TEXT    NOT NULL,   -- "success" | "denied"
    reason              TEXT,               -- populated on denial
    approved_exception  INTEGER NOT NULL DEFAULT 0,  -- 1 if approved outside policy allowlist
    exception_detail    TEXT                -- domain / skill / server that was the exception
);

CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_tool    ON audit_events(tool, id DESC);

-- Work unit registry.
-- A work unit is the set of changes on a branch since the previous push.
-- Keyed on (repo_url, base_branch, base_commit). Closes when git push fires.
CREATE TABLE IF NOT EXISTS work_units (
    id           TEXT    PRIMARY KEY,   -- UUID
    repo_url     TEXT    NOT NULL,
    base_branch  TEXT    NOT NULL,      -- branch name at session open
    base_commit  TEXT    NOT NULL,      -- HEAD when unit opened (last pushed commit)
    head_branch  TEXT,                  -- branch pushed to (may differ if dev switched mid-session)
    head_commit  TEXT,                  -- populated at push (NULL = still open)
    opened_at    INTEGER NOT NULL,      -- Unix ms
    pushed_at    INTEGER,               -- Unix ms (set when payload is POSTed)
    status       TEXT    NOT NULL DEFAULT 'open',  -- open | pushed | abandoned
    prompt       TEXT    NOT NULL,      -- first user message since base_commit
    narrative    TEXT,                  -- assistant summary at push time
    qa_result    TEXT,                  -- JSON: {checks, passed} for result.qa
    UNIQUE (repo_url, base_branch, base_commit)
);

-- Maps sessions to their work unit (many sessions may contribute to one unit).
CREATE TABLE IF NOT EXISTS work_unit_sessions (
    work_unit_id  TEXT  NOT NULL REFERENCES work_units(id),
    session_id    TEXT  NOT NULL REFERENCES tasks(session_id),
    PRIMARY KEY (work_unit_id, session_id)
);

-- Policy snapshot per session start within a work unit.
-- One row per session where the manifest differed from the previous session.
CREATE TABLE IF NOT EXISTS policy_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    work_unit_id   TEXT    NOT NULL REFERENCES work_units(id),
    session_id     TEXT    NOT NULL REFERENCES tasks(session_id),
    manifest_hash  TEXT    NOT NULL,
    manifest_text  TEXT    NOT NULL,   -- full YAML of /etc/contained/manifest.yaml
    provenance     TEXT    NOT NULL,   -- provenance snapshot at container startup
    captured_at    INTEGER NOT NULL,   -- Unix ms (taken immediately at session open)
    policy_ref     TEXT,               -- mainlined.policy_ref from manifest (mAInlined git SHA)
    policy_version TEXT                -- mainlined.policy_version from manifest
);

CREATE INDEX IF NOT EXISTS idx_policy_snapshots_unit ON policy_snapshots(work_unit_id);

-- Proof-ready unified timeline. Populated at push time by build_actions().
-- Directly maps to outcome.result.actions[] in the ATP payload.
CREATE TABLE IF NOT EXISTS actions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    work_unit_id       TEXT    NOT NULL REFERENCES work_units(id),
    session_id         TEXT    NOT NULL REFERENCES tasks(session_id),
    seq                INTEGER NOT NULL,   -- global order within the work unit
    ts                 TEXT,               -- ISO-8601
    action_type        TEXT    NOT NULL,
    -- 'user_message' | 'assistant_response' | 'tool_call' | 'skill_use'
    -- 'operator_shell' | 'context_compaction'

    -- tool_call / skill_use fields
    tool_name          TEXT,
    tool_input         TEXT,               -- JSON
    tool_outcome       TEXT,               -- success | denied | permission_requested
    tool_reason        TEXT,
    approved_exception INTEGER,            -- 1 if operator approved out-of-policy
    exception_detail   TEXT,
    output_hash        TEXT    REFERENCES blobs(hash),

    -- file write enrichment (subset of tool_call)
    file_path          TEXT,
    before_hash        TEXT,               -- NULL for new files
    after_hash         TEXT,

    -- message fields
    content_hash       TEXT    REFERENCES blobs(hash),
    content_short      TEXT,               -- first 500 chars

    UNIQUE (work_unit_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id);
"""

# Tools whose content is fully captured in the blobs/snapshots tables.
# audit_events stores only the file path as a back-reference.
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Read-only tools: no trace unit worth storing.
_READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}


def extract_narrative_from_transcript(transcript_path: str) -> str:
    """
    Return the agent's final human-readable narrative from *transcript_path*.

    Reads the JSONL transcript file produced by Claude Code, walks entries in
    reverse, and extracts the concatenated text from the last visible assistant
    message that contains at least one TextBlock.

    Returns an empty string if the transcript is missing, unreadable, or
    contains no qualifying assistant message.

    For richer structured extraction (thinking blocks + reasoning steps +
    closing statement) use :func:`extract_session_narrative` instead.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.get("type") != "assistant":
            continue
        # Skip meta / sidechain entries (tool-result sidechains, compaction notices).
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        content = message.get("content") or []
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    texts.append(text)
        narrative = "\n\n".join(texts).strip()
        if narrative:
            return narrative

    return ""


def extract_tool_outputs_from_transcript(transcript_path: str) -> list[dict]:
    """
    Return a list of tool call records from the Claude Code session transcript.

    Walks the JSONL transcript forward, pairing ``tool_use`` blocks in
    assistant messages with their corresponding ``tool_result`` entries in
    subsequent user-turn messages.

    Each returned record::

        {
            "tool_use_id": str,
            "tool_name":   str,
            "input":       dict,
            "output":      str,       # full text output (untruncated)
            "exit_code":   int|None,
        }

    Returns an empty list if the transcript is missing, unreadable, or
    contains no paired tool calls.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    # tool_use_id → {tool_name, input} for calls awaiting their result
    pending: dict[str, dict] = {}
    results: list[dict] = []

    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            continue

        entry_type = entry.get("type")

        # ── Collect tool_use blocks from assistant messages ────────────────
        if entry_type == "assistant":
            if entry.get("isMeta") or entry.get("isSidechain"):
                continue
            message = entry.get("message") or {}
            content = message.get("content") or []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tid = block.get("id") or ""
                    if tid:
                        pending[tid] = {
                            "tool_name": block.get("name") or "",
                            "input": block.get("input") or {},
                        }

        # ── Match tool_result entries (appear in user turns) ───────────────
        elif entry_type == "user":
            message = entry.get("message") or {}
            content = message.get("content") or []
            if isinstance(content, str):
                continue  # plain text user message, not a tool result
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id") or ""
                if not tid or tid not in pending:
                    continue

                pend = pending.pop(tid)
                raw = block.get("content") or ""

                # Normalise output to a plain string
                if isinstance(raw, list):
                    output = "\n".join(
                        b.get("text", "")
                        for b in raw
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    output = str(raw)

                # exit_code may be embedded at the block level or inside content
                exit_code: int | None = block.get("exit_code")
                if exit_code is None and isinstance(raw, list):
                    for b in raw:
                        if isinstance(b, dict) and b.get("exit_code") is not None:
                            exit_code = b["exit_code"]
                            break

                results.append(
                    {
                        "tool_use_id": tid,
                        "tool_name": pend["tool_name"],
                        "input": pend["input"],
                        "output": output,
                        "exit_code": exit_code,
                    }
                )

    return results


def extract_session_narrative(transcript_path: str) -> dict:
    """
    Build a structured narrative from the Claude Code session transcript.

    Makes a single forward pass and collects three kinds of content:

    * **thinking_excerpts** — ``thinking`` blocks from assistant messages.
      These are Claude's internal reasoning: what it observed, what
      alternatives it considered, what tradeoffs it weighed.

    * **reasoning_steps** — text blocks that immediately precede a
      ``tool_use`` block within the same assistant message.  These are
      Claude's stated intent just before each action.

    * **closings** — list of text from every non-meta assistant message that
      contains no ``tool_use`` blocks, one entry per turn.  Across a
      multi-turn session this captures the agent's reply at the end of each
      turn rather than only the last one.

    Returns an empty dict ``{}`` on any failure or if the transcript
    contains no qualifying content.  Callers should persist this as JSON
    in ``tasks.narrative`` so it is queryable via ``json_extract()``.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}

    thinking_excerpts: list[str] = []
    reasoning_steps: list[dict] = []
    closings: list[str] = []

    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            continue

        if entry.get("type") != "assistant":
            continue
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue

        message = entry.get("message") or {}
        content = message.get("content") or []
        if not isinstance(content, list):
            continue

        has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)

        pending_text: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "thinking":
                excerpt = (block.get("thinking") or "").strip()
                if excerpt:
                    thinking_excerpts.append(excerpt)
                pending_text = []  # thinking resets pre-tool text accumulation

            elif btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    pending_text.append(text)

            elif btype == "tool_use":
                # Flush accumulated text as a reasoning step for this tool call
                if pending_text:
                    reasoning_steps.append(
                        {
                            "before_tool": block.get("name") or "",
                            "tool_input": {
                                k: (str(v)[:200] if v is not None else None)
                                for k, v in (block.get("input") or {}).items()
                                if k in ("command", "file_path", "description", "prompt")
                            },
                            "rationale": " ".join(pending_text),
                        }
                    )
                pending_text = []

        # Messages with no tool_use are narrative text; collect all of them across turns
        if not has_tool_use:
            text_parts = [
                (b.get("text") or "").strip()
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            combined = " ".join(t for t in text_parts if t)
            if combined:
                closings.append(combined)

    if not thinking_excerpts and not reasoning_steps and not closings:
        return {}

    return {
        "thinking_excerpts": thinking_excerpts,
        "reasoning_steps": reasoning_steps,
        "closings": closings,
    }


# Per-db-path lock that serialises schema initialisation within a process.
# Concurrent hook subprocesses each have their own Python interpreter so this
# only helps the in-process (same-process, multi-thread) case; cross-process
# contention is handled by the C-level busy_timeout on sqlite3.connect().
_INIT_LOCKS: dict[str, _threading.Lock] = {}
_INIT_LOCKS_LOCK = _threading.Lock()


def _get_init_lock(db_path: str) -> _threading.Lock:
    with _INIT_LOCKS_LOCK:
        if db_path not in _INIT_LOCKS:
            _INIT_LOCKS[db_path] = _threading.Lock()
        return _INIT_LOCKS[db_path]


class contAInedTracer:
    """
    Core write-tracking and task-review engine.

    Instantiate once per process with the path to `.contAIned/tracer.db`.
    The database is created (with WAL mode) if it does not exist.
    All public methods are safe to call from concurrent hook subprocesses:
    SQLite WAL serialises writers; INSERT OR IGNORE guards idempotent paths.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # Serialise schema init within the same process so that two threads
        # racing to open the same DB don't collide on CREATE TABLE / ALTER TABLE.
        # Cross-process contention is handled by the C-level timeout=30 below.
        with _get_init_lock(db_path):
            self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Create all tables and indexes if they do not already exist.

        Each DDL statement is executed outside a Python-managed transaction
        (i.e. in autocommit mode) so that SQLite's C-level busy-wait retries
        on lock contention.  ``with self.conn:`` issues BEGIN IMMEDIATE, which
        bypasses the busy_timeout retry loop and raises immediately if another
        connection holds the write lock — the opposite of what we want during
        concurrent hook-subprocess initialisation.
        """
        statements = [s.strip() for s in _SCHEMA_SQL.split(";") if s.strip()]
        for stmt in statements:
            self.conn.execute(stmt)
        self._migrate()

    def _migrate(self) -> None:
        """Apply incremental schema migrations to existing databases.

        Each ALTER TABLE is executed outside a Python-managed transaction so
        that SQLite's C-level busy-wait (set via PRAGMA busy_timeout) retries
        on lock contention rather than immediately raising OperationalError.
        ``with self.conn:`` issues BEGIN IMMEDIATE which bypasses the busy-wait
        for DDL statements; executing directly in autocommit mode avoids that.

        Duplicate-column errors ("duplicate column name") are silently ignored
        so the method is idempotent against databases that already have the
        column.
        """
        migrations = [
            "ALTER TABLE audit_events ADD COLUMN approved_exception INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE audit_events ADD COLUMN exception_detail TEXT",
            "ALTER TABLE snapshots ADD COLUMN diff_hash TEXT REFERENCES blobs(hash)",
            "ALTER TABLE tasks ADD COLUMN transcript_path TEXT",
            "ALTER TABLE work_units RENAME COLUMN branch TO base_branch",
            "ALTER TABLE work_units ADD COLUMN head_branch TEXT",
            # Phase 2 — mAInlined policy binding in proof chain
            "ALTER TABLE policy_snapshots ADD COLUMN policy_ref TEXT",
            "ALTER TABLE policy_snapshots ADD COLUMN policy_version TEXT",
            # Phase 2 — ATP Exchange submission timestamps
            "ALTER TABLE tasks ADD COLUMN atp_depot_submitted_at INTEGER",
            "ALTER TABLE tasks ADD COLUMN atp_committed_at INTEGER",
        ]
        for sql in migrations:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists — idempotent

    # ------------------------------------------------------------------
    # Blob store (internal)
    # ------------------------------------------------------------------

    def _store_blob(self, content: bytes) -> str:
        """
        Store *content* in the blob table (content-addressed, deduplicated).

        Returns the SHA-256 hex digest that identifies the blob.
        INSERT OR IGNORE ensures identical content is stored exactly once.
        """
        blob_hash = hashlib.sha256(content).hexdigest()
        compressed = zlib.compress(content, level=1)
        now_ms = int(time.time() * 1000)
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO blobs (hash, content, size_bytes, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (blob_hash, compressed, len(content), now_ms),
            )
        return blob_hash

    def _retrieve_blob(self, blob_hash: str) -> bytes:
        """Return decompressed content for *blob_hash*. Raises KeyError if missing."""
        row = self.conn.execute("SELECT content FROM blobs WHERE hash = ?", (blob_hash,)).fetchone()
        if row is None:
            raise KeyError(f"Blob not found: {blob_hash}")
        return zlib.decompress(row[0])

    # ------------------------------------------------------------------
    # Baseline capture  (called from tracer_pre.py)
    # ------------------------------------------------------------------

    def capture_baseline(self, session_id: str, file_path: str) -> Optional[str]:
        """
        Record the file's current content as the pre-task baseline for *session_id*.

        Called by the PreToolUse hook before the first Write/Edit/MultiEdit to
        a file within a session.  INSERT OR IGNORE makes this idempotent — only
        the first call per (session_id, file_path) has any effect.

        Returns the baseline blob hash, or None if the file did not exist.
        """
        now_ms = int(time.time() * 1000)
        path = Path(file_path)

        if not path.exists():
            with self.conn:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO baselines
                        (session_id, file_path, pre_hash, captured_at)
                    VALUES (?, ?, NULL, ?)
                    """,
                    (session_id, file_path, now_ms),
                )
            return None

        content = path.read_bytes()
        blob_hash = self._store_blob(content)

        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO baselines
                    (session_id, file_path, pre_hash, captured_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, file_path, blob_hash, now_ms),
            )

        return blob_hash

    # ------------------------------------------------------------------
    # Snapshot recording  (called from tracer_post.py)
    # ------------------------------------------------------------------

    def track_write(
        self,
        session_id: str,
        file_path: str,
        content: bytes,
        metadata: Optional[dict] = None,
    ) -> str:
        """
        Record that *session_id* wrote *content* to *file_path*.

        The caller (tracer_post.py) reads the file from disk after the write
        so that the stored blob always reflects what is actually on disk.
        Computes a unified diff from the baseline blob and caches it as
        ``snapshots.diff_hash`` for fast payload assembly.
        Returns the blob hash.
        """
        blob_hash = self._store_blob(content)
        now_ms = int(time.time() * 1000)
        diff_hash = self._compute_snapshot_diff(session_id, file_path, blob_hash)

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO snapshots
                    (session_id, file_path, blob_hash, written_at, metadata, diff_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    file_path,
                    blob_hash,
                    now_ms,
                    json.dumps(metadata) if metadata else None,
                    diff_hash,
                ),
            )

        return blob_hash

    def _compute_snapshot_diff(
        self,
        session_id: str,
        file_path: str,
        new_blob_hash: str,
    ) -> Optional[str]:
        """
        Compute a unified diff from the baseline to the new blob for *file_path*
        and return the blob hash of the stored diff text.

        Uses the baseline captured for *session_id* (not the full tree) to keep
        this fast at write time.  Returns None if no baseline exists (possible
        when the baseline hook hasn't fired yet) or if the content is binary.
        """
        try:
            bl = self.conn.execute(
                "SELECT pre_hash FROM baselines WHERE session_id = ? AND file_path = ?",
                (session_id, file_path),
            ).fetchone()
            if bl is None:
                return None  # baseline not yet captured

            pre_hash = bl[0]
            new_content = self._retrieve_blob(new_blob_hash)

            # Heuristic binary check: NUL byte in first 8 KB
            if b"\x00" in new_content[:8192]:
                return None

            after_lines = new_content.decode("utf-8", errors="replace").splitlines()

            if pre_hash is None:
                before_lines: list[str] = []
            else:
                old_content = self._retrieve_blob(pre_hash)
                before_lines = old_content.decode("utf-8", errors="replace").splitlines()

            diff = "\n".join(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=f"a/{file_path}",
                    tofile=f"b/{file_path}",
                    lineterm="",
                )
            )
            if not diff:
                return None
            return self._store_blob(diff.encode("utf-8"))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Audit event logging  (called from audit.py)
    # ------------------------------------------------------------------

    def log_event(
        self,
        session_id: str,
        tool: str,
        tool_input: Optional[dict],
        outcome: str,
        reason: Optional[str] = None,
        tool_response: Optional[dict] = None,
        approved_exception: bool = False,
        exception_detail: Optional[str] = None,
    ) -> None:
        """
        Append one audit event for a tool call.

        Extracts a tool-specific trace unit from *tool_input* / *tool_response*
        and stores it as JSON in the ``input`` column:

        - Write / Edit / MultiEdit → ``{"file_path": ...}`` back-reference only
          (full content lives in the blob store via track_write).
        - Bash → ``{"command": ..., "exit_code": ..., "stdout_head": ...}``
          with stdout capped at 500 characters.
        - Agent → ``{"agent_type": ..., "prompt_head": ...}``
          with prompt capped at 200 characters.
        - Read / Glob / Grep → ``None`` (read-only; no diff value).
        - All other tools → first 5 keys, values truncated to 200 chars.

        Never raises — audit must not block agent execution.
        """
        trace_unit = self._extract_trace_unit(tool, tool_input, tool_response)
        ts = datetime.now(timezone.utc).isoformat()
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO audit_events
                        (ts, session_id, tool, input, outcome, reason,
                         approved_exception, exception_detail)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        session_id,
                        tool,
                        json.dumps(trace_unit) if trace_unit is not None else None,
                        outcome,
                        reason,
                        1 if approved_exception else 0,
                        exception_detail,
                    ),
                )
        except Exception:
            pass  # never block execution due to logging failure

    def _extract_trace_unit(
        self,
        tool: str,
        tool_input: Optional[dict],
        tool_response: Optional[dict],
    ) -> Optional[dict]:
        """Return the tool-specific trace unit to store in audit_events.input."""
        inp = tool_input or {}

        if tool in _WRITE_TOOLS:
            # Full content captured via track_write / blob store; store only path.
            if tool == "MultiEdit":
                edits = inp.get("edits") or []
                paths = list({e.get("file_path") for e in edits if e.get("file_path")})
                return {"file_paths": paths}
            return {"file_path": inp.get("file_path")}

        if tool in _READ_ONLY_TOOLS:
            path = inp.get("file_path") or inp.get("pattern") or inp.get("path")
            return {"file_path": path} if path else None

        if tool == "Bash":
            resp = tool_response or {}
            raw_stdout: str = resp.get("stdout") or resp.get("output") or ""
            return {
                "command": inp.get("command"),
                "exit_code": resp.get("exit_code"),
                "stdout_head": raw_stdout[:500],
            }

        if tool == "Agent":
            prompt: str = inp.get("prompt") or inp.get("description") or ""
            return {
                "agent_type": inp.get("subagent_type") or inp.get("agent_type"),
                "prompt_head": prompt[:200],
            }

        # Fallback: first 5 keys, values truncated to 200 chars.
        fallback: dict = {}
        for i, (k, v) in enumerate(inp.items()):
            if i >= 5:
                break
            fallback[k] = str(v)[:200] if v is not None else None
        return fallback if fallback else None

    # ------------------------------------------------------------------
    # Task registry
    # ------------------------------------------------------------------

    def open_task(
        self,
        session_id: str,
        prompt: str,
        parent_session_id: Optional[str] = None,
    ) -> None:
        """
        Register a new task as ``open``, or reopen it if it already exists.

        On first call for a session_id the row is created.  On subsequent
        calls (e.g. the user sends a follow-up in a multi-turn session) the
        status is reset to ``open`` so the task accurately reflects that the
        agent is actively working again.  The original prompt, timestamps, and
        parent linkage are preserved.
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO tasks
                    (session_id, parent_session_id, prompt, status, started_at)
                VALUES (?, ?, ?, 'open', ?)
                ON CONFLICT(session_id) DO UPDATE SET status = 'open'
                """,
                (session_id, parent_session_id, prompt, int(time.time() * 1000)),
            )

    def set_task_status(
        self,
        session_id: str,
        status: str,
        summary: Optional[dict] = None,
    ) -> None:
        """
        Transition *session_id*'s task to *status*.

        Optionally attaches a diff summary (JSON-serialisable dict) to
        ``tasks.summary``.  Sets ``ended_at`` to now.

        ``COALESCE(?, col)`` semantics: passing ``None`` for *summary*
        preserves whatever value is already stored in the DB.
        Pass an explicit value to overwrite.
        """
        now_ms = int(time.time() * 1000)
        with self.conn:
            self.conn.execute(
                """
                UPDATE tasks
                SET status   = ?,
                    ended_at = ?,
                    summary  = COALESCE(?, summary)
                WHERE session_id = ?
                """,
                (
                    status,
                    now_ms,
                    json.dumps(summary) if summary is not None else None,
                    session_id,
                ),
            )

    # ------------------------------------------------------------------
    # Work unit lifecycle
    # ------------------------------------------------------------------

    def open_or_find_work_unit(
        self,
        repo_url: str,
        base_branch: str,
        base_commit: str,
        prompt: str,
    ) -> str:
        """
        Return the id of the open work unit for *(repo_url, base_branch, base_commit)*,
        creating it if it does not yet exist.

        The ``prompt`` is stored only on creation — if the unit already exists
        (resumed session), the original prompt is preserved.
        """
        now_ms = int(time.time() * 1000)
        unit_id = str(uuid.uuid4())
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO work_units
                    (id, repo_url, base_branch, base_commit, opened_at, status, prompt)
                VALUES (?, ?, ?, ?, ?, 'open', ?)
                """,
                (unit_id, repo_url, base_branch, base_commit, now_ms, prompt),
            )
            # If the unit already existed with a blank prompt (written at session
            # startup before the first user message was known), fill it in now.
            if prompt:
                self.conn.execute(
                    """
                    UPDATE work_units SET prompt = ?
                    WHERE repo_url = ? AND base_branch = ? AND base_commit = ?
                      AND (prompt IS NULL OR prompt = '')
                    """,
                    (prompt, repo_url, base_branch, base_commit),
                )
        row = self.conn.execute(
            "SELECT id FROM work_units WHERE repo_url = ? AND base_branch = ? AND base_commit = ?",
            (repo_url, base_branch, base_commit),
        ).fetchone()
        return row[0]

    def register_session_in_work_unit(
        self,
        work_unit_id: str,
        session_id: str,
    ) -> None:
        """Link *session_id* to *work_unit_id* (idempotent)."""
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO work_unit_sessions (work_unit_id, session_id) VALUES (?, ?)",
                (work_unit_id, session_id),
            )

    def record_policy_snapshot(
        self,
        work_unit_id: str,
        session_id: str,
        manifest_path: str,
        provenance_path: str,
    ) -> None:
        """
        Capture the current manifest and provenance snapshot for this session.

        Reads the manifest YAML from *manifest_path* and the provenance YAML
        from *provenance_path*.  If either file is missing an empty string is
        stored so the row is always written.
        """
        try:
            manifest_text = Path(manifest_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            manifest_text = ""
        manifest_hash = hashlib.sha256(manifest_text.encode()).hexdigest()

        # Extract mAInlined policy_ref and policy_version for proof chain binding.
        policy_ref: str = ""
        policy_version: str = ""
        try:
            manifest_data = yaml.safe_load(manifest_text) or {}
            mainlined = manifest_data.get("mainlined") or {}
            policy_ref = mainlined.get("policy_ref") or ""
            policy_version = mainlined.get("policy_version") or ""
        except Exception:
            pass

        try:
            provenance_text = Path(provenance_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            provenance_text = ""

        now_ms = int(time.time() * 1000)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO policy_snapshots
                    (work_unit_id, session_id, manifest_hash,
                     manifest_text, provenance, captured_at,
                     policy_ref, policy_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_unit_id,
                    session_id,
                    manifest_hash,
                    manifest_text,
                    provenance_text,
                    now_ms,
                    policy_ref or None,
                    policy_version or None,
                ),
            )

    def record_qa_result(self, work_unit_id: str, result_dict: dict) -> None:
        """Store *result_dict* as the QA result for *work_unit_id*."""
        with self.conn:
            self.conn.execute(
                "UPDATE work_units SET qa_result = ? WHERE id = ?",
                (json.dumps(result_dict), work_unit_id),
            )

    def record_narrative(self, work_unit_id: str, narrative: str) -> None:
        """Store *narrative* in ``work_units.narrative`` for *work_unit_id*."""
        with self.conn:
            self.conn.execute(
                "UPDATE work_units SET narrative = ? WHERE id = ?",
                (narrative, work_unit_id),
            )

    def get_active_work_unit(self, session_id: str) -> Optional[str]:
        """
        Return the work_unit_id of the open work unit associated with
        *session_id* or any of its ancestor sessions, or None if not found.

        Walks up the parent_session_id chain so post-compaction sessions
        (which are not directly registered in work_unit_sessions) are still
        linked to the work unit opened by the original session.
        """
        row = self.conn.execute(
            """
            WITH RECURSIVE ancestors(sid) AS (
                SELECT ? AS sid
                UNION ALL
                SELECT t.parent_session_id
                FROM tasks t
                JOIN ancestors ON t.session_id = ancestors.sid
                WHERE t.parent_session_id IS NOT NULL
            )
            SELECT wu.id FROM work_units wu
            JOIN work_unit_sessions wus ON wus.work_unit_id = wu.id
            JOIN ancestors a ON a.sid = wus.session_id
            WHERE wu.status = 'open'
            ORDER BY wu.opened_at DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return row[0] if row else None

    def complete_work_unit(
        self,
        work_unit_id: str,
        head_commit: str,
        head_branch: Optional[str] = None,
    ) -> None:
        """Mark *work_unit_id* as pushed, recording *head_commit* and *head_branch*.

        *head_branch* is the branch that was actually pushed to — it may differ
        from *base_branch* if the developer switched branches mid-session.
        Sets ``pushed_at`` to now.
        """
        now_ms = int(time.time() * 1000)
        with self.conn:
            self.conn.execute(
                """
                UPDATE work_units
                SET status = 'pushed', pushed_at = ?, head_commit = ?, head_branch = ?
                WHERE id = ?
                """,
                (now_ms, head_commit, head_branch, work_unit_id),
            )

    # ------------------------------------------------------------------
    # Actions timeline builder
    # ------------------------------------------------------------------

    def build_actions(
        self,
        work_unit_id: str,
        transcript_paths: list[str],
    ) -> None:
        """
        Populate the ``actions`` table for *work_unit_id* from the session
        transcripts and ``audit_events``.

        Parses each JSONL transcript in *transcript_paths* in order, creates
        action rows for user messages, assistant responses, and tool calls,
        then merges ``audit_events`` for policy outcomes (denials, approved
        exceptions) and enriches file-change actions with before/after blob
        hashes from ``snapshots`` and ``baselines``.

        Rows are inserted with ``INSERT OR IGNORE`` so the method is safe to
        call multiple times (e.g. after a retry).
        """
        # ── Collect session ids for this work unit ─────────────────────
        session_ids: list[str] = [
            r[0]
            for r in self.conn.execute(
                "SELECT session_id FROM work_unit_sessions WHERE work_unit_id = ?",
                (work_unit_id,),
            ).fetchall()
        ]
        default_session = session_ids[0] if session_ids else "unknown"

        # ── Build audit event lookup (session_id, tool) → [events] ─────
        audit_lookup: dict[tuple, list[dict]] = {}
        if session_ids:
            ph = ",".join("?" * len(session_ids))
            for row in self.conn.execute(
                f"""
                SELECT ts, session_id, tool, input, outcome, reason,
                       approved_exception, exception_detail
                FROM audit_events
                WHERE session_id IN ({ph})
                ORDER BY id
                """,
                session_ids,
            ).fetchall():
                key = (row[1], row[2])
                audit_lookup.setdefault(key, []).append(
                    {
                        "ts": row[0],
                        "outcome": row[4],
                        "reason": row[5],
                        "approved_exception": row[6],
                        "exception_detail": row[7],
                    }
                )

        # ── Build baseline lookup (file_path → pre_hash) ───────────────
        baseline_lookup: dict[str, Optional[str]] = {}
        if session_ids:
            ph = ",".join("?" * len(session_ids))
            for row in self.conn.execute(
                f"""
                SELECT file_path, pre_hash FROM baselines
                WHERE session_id IN ({ph})
                ORDER BY captured_at ASC
                """,
                session_ids,
            ).fetchall():
                if row[0] not in baseline_lookup:
                    baseline_lookup[row[0]] = row[1]

        # ── Build snapshot lookup (file_path → [blob_hash]) ────────────
        snapshot_lookup: dict[str, list[str]] = {}
        if session_ids:
            ph = ",".join("?" * len(session_ids))
            for row in self.conn.execute(
                f"""
                SELECT file_path, blob_hash FROM snapshots
                WHERE session_id IN ({ph})
                ORDER BY written_at, id
                """,
                session_ids,
            ).fetchall():
                snapshot_lookup.setdefault(row[0], []).append(row[1])

        # ── Parse transcripts ───────────────────────────────────────────
        actions: list[dict] = []
        seq = 0
        audit_use_counts: dict[tuple, int] = {}

        for transcript_path in transcript_paths:
            if not transcript_path:
                continue
            p = Path(transcript_path)
            if not p.exists():
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            # Pending operator shell escape — populated by <bash-input>, flushed
            # by the following <bash-stdout> entry or at end of transcript.
            pending_shell: dict | None = None

            for raw_line in lines:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue

                entry_type = entry.get("type")
                entry_ts = entry.get("timestamp") or entry.get("ts")
                # sessionId in transcript uses camelCase
                cur_session = entry.get("sessionId") or entry.get("session_id") or default_session

                if entry_type == "user":
                    # Skip meta entries (caveats, stop-hook feedback, etc.)
                    if entry.get("isMeta"):
                        continue

                    message = entry.get("message") or {}
                    content = message.get("content") or []

                    if isinstance(content, str):
                        # ── Operator shell escape (! command) ─────────────────
                        if content.startswith("<bash-input>"):
                            cmd = content[len("<bash-input>") :]
                            if cmd.endswith("</bash-input>"):
                                cmd = cmd[: -len("</bash-input>")]
                            pending_shell = {
                                "work_unit_id": work_unit_id,
                                "session_id": cur_session,
                                "ts": entry_ts,
                                "command": cmd.strip(),
                            }
                            continue

                        if content.startswith("<bash-stdout>") and pending_shell is not None:
                            stdout_m = re.search(
                                r"<bash-stdout>(.*?)</bash-stdout>", content, re.DOTALL
                            )
                            stderr_m = re.search(
                                r"<bash-stderr>(.*?)</bash-stderr>", content, re.DOTALL
                            )
                            stdout = stdout_m.group(1).strip() if stdout_m else ""
                            stderr = stderr_m.group(1).strip() if stderr_m else ""
                            cmd = pending_shell["command"]
                            out_parts = ["! " + cmd]
                            if stdout:
                                out_parts.append(stdout[:400])
                            if stderr:
                                out_parts.append("[stderr] " + stderr[:200])
                            seq += 1
                            actions.append(
                                {
                                    "work_unit_id": pending_shell["work_unit_id"],
                                    "session_id": pending_shell["session_id"],
                                    "seq": seq,
                                    "ts": pending_shell["ts"],
                                    "action_type": "operator_shell",
                                    "tool_name": "OperatorShell",
                                    "tool_input": json.dumps(
                                        {"command": cmd, "stdout": stdout, "stderr": stderr}
                                    ),
                                    "tool_outcome": "executed",
                                    "content_short": "\n".join(out_parts)[:500],
                                }
                            )
                            pending_shell = None
                            continue

                        # Flush any pending shell without output (edge case)
                        if pending_shell is not None:
                            cmd = pending_shell["command"]
                            seq += 1
                            actions.append(
                                {
                                    "work_unit_id": pending_shell["work_unit_id"],
                                    "session_id": pending_shell["session_id"],
                                    "seq": seq,
                                    "ts": pending_shell["ts"],
                                    "action_type": "operator_shell",
                                    "tool_name": "OperatorShell",
                                    "tool_input": json.dumps(
                                        {"command": cmd, "stdout": "", "stderr": ""}
                                    ),
                                    "tool_outcome": "executed",
                                    "content_short": ("! " + cmd)[:500],
                                }
                            )
                            pending_shell = None

                        text = content
                    else:
                        # Flush pending shell before a non-string user entry
                        if pending_shell is not None:
                            cmd = pending_shell["command"]
                            seq += 1
                            actions.append(
                                {
                                    "work_unit_id": pending_shell["work_unit_id"],
                                    "session_id": pending_shell["session_id"],
                                    "seq": seq,
                                    "ts": pending_shell["ts"],
                                    "action_type": "operator_shell",
                                    "tool_name": "OperatorShell",
                                    "tool_input": json.dumps(
                                        {"command": cmd, "stdout": "", "stderr": ""}
                                    ),
                                    "tool_outcome": "executed",
                                    "content_short": ("! " + cmd)[:500],
                                }
                            )
                            pending_shell = None

                        text = ""
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = (block.get("text") or "").strip()
                                break

                    if text.strip():
                        seq += 1
                        actions.append(
                            {
                                "work_unit_id": work_unit_id,
                                "session_id": cur_session,
                                "seq": seq,
                                "ts": entry_ts,
                                "action_type": "user_message",
                                "content_short": text[:500],
                            }
                        )

                elif entry_type == "assistant":
                    if entry.get("isMeta") or entry.get("isSidechain"):
                        continue
                    message = entry.get("message") or {}
                    content = message.get("content") or []
                    if not isinstance(content, list):
                        continue

                    text_parts: list[str] = []
                    tool_calls: list[dict] = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            t = (block.get("text") or "").strip()
                            if t:
                                text_parts.append(t)
                        elif btype == "tool_use":
                            tool_calls.append(block)

                    if text_parts:
                        combined = "\n".join(text_parts)
                        seq += 1
                        actions.append(
                            {
                                "work_unit_id": work_unit_id,
                                "session_id": cur_session,
                                "seq": seq,
                                "ts": entry_ts,
                                "action_type": "assistant_response",
                                "content_short": combined[:500],
                            }
                        )

                    for tool_block in tool_calls:
                        tool_name = tool_block.get("name") or ""
                        tool_input = tool_block.get("input") or {}

                        audit_key = (cur_session, tool_name)
                        audit_events_list = audit_lookup.get(audit_key, [])
                        use_idx = audit_use_counts.get(audit_key, 0)
                        audit_ev = (
                            audit_events_list[use_idx] if use_idx < len(audit_events_list) else None
                        )
                        if audit_ev:
                            audit_use_counts[audit_key] = use_idx + 1

                        outcome = audit_ev["outcome"] if audit_ev else "success"
                        reason = audit_ev["reason"] if audit_ev else None
                        approved_exception = audit_ev["approved_exception"] if audit_ev else 0
                        exception_detail = audit_ev["exception_detail"] if audit_ev else None

                        seq += 1
                        action: dict = {
                            "work_unit_id": work_unit_id,
                            "session_id": cur_session,
                            "seq": seq,
                            "ts": entry_ts,
                            "action_type": "skill_use" if tool_name == "Skill" else "tool_call",
                            "tool_name": tool_name,
                            "tool_input": json.dumps(tool_input),
                            "tool_outcome": outcome,
                            "tool_reason": reason,
                            "approved_exception": approved_exception,
                            "exception_detail": exception_detail,
                        }

                        # Enrich write tool actions with file hashes
                        if tool_name in _WRITE_TOOLS:
                            fp = tool_input.get("file_path")
                            if tool_name == "MultiEdit":
                                edits = tool_input.get("edits") or []
                                fps = list(
                                    {e.get("file_path") for e in edits if e.get("file_path")}
                                )
                                fp = fps[0] if fps else None
                            if fp:
                                action["file_path"] = fp
                                action["before_hash"] = baseline_lookup.get(fp)
                                snaps = snapshot_lookup.get(fp, [])
                                if snaps:
                                    action["after_hash"] = snaps[-1]

                        actions.append(action)

                elif entry_type == "system":
                    # ── Context compaction boundary ────────────────────────
                    if entry.get("subtype") == "compact_boundary":
                        meta = entry.get("compactMetadata") or {}
                        trigger = meta.get("trigger", "auto")
                        pre_tokens = meta.get("preTokens", "?")
                        seq += 1
                        actions.append(
                            {
                                "work_unit_id": work_unit_id,
                                "session_id": cur_session,
                                "seq": seq,
                                "ts": entry_ts,
                                "action_type": "context_compaction",
                                "content_short": (
                                    f"Conversation compacted "
                                    f"(trigger={trigger}, preTokens={pre_tokens})"
                                ),
                            }
                        )

            # Flush any pending operator shell at end of transcript
            if pending_shell is not None:
                cmd = pending_shell["command"]
                seq += 1
                actions.append(
                    {
                        "work_unit_id": pending_shell["work_unit_id"],
                        "session_id": pending_shell["session_id"],
                        "seq": seq,
                        "ts": pending_shell["ts"],
                        "action_type": "operator_shell",
                        "tool_name": "OperatorShell",
                        "tool_input": json.dumps({"command": cmd, "stdout": "", "stderr": ""}),
                        "tool_outcome": "executed",
                        "content_short": ("! " + cmd)[:500],
                    }
                )
                pending_shell = None

        # ── Write actions to DB ─────────────────────────────────────────
        if actions:
            with self.conn:
                for a in actions:
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO actions
                            (work_unit_id, session_id, seq, ts, action_type,
                             tool_name, tool_input, tool_outcome, tool_reason,
                             approved_exception, exception_detail,
                             file_path, before_hash, after_hash,
                             content_short)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            a["work_unit_id"],
                            a["session_id"],
                            a["seq"],
                            a.get("ts"),
                            a["action_type"],
                            a.get("tool_name"),
                            a.get("tool_input"),
                            a.get("tool_outcome"),
                            a.get("tool_reason"),
                            a.get("approved_exception"),
                            a.get("exception_detail"),
                            a.get("file_path"),
                            a.get("before_hash"),
                            a.get("after_hash"),
                            a.get("content_short"),
                        ),
                    )

    # ------------------------------------------------------------------
    # Payload assembly
    # ------------------------------------------------------------------

    def assemble_payload(self, work_unit_id: str) -> dict:
        """
        Assemble and return the ``{invocation, outcome}`` ATP payload for
        *work_unit_id*.

        Reads from ``work_units``, ``policy_snapshots``, ``snapshots``,
        ``baselines``, ``blobs``, and ``actions``.  Diff text is read from the
        cached ``blobs`` entry referenced by ``snapshots.diff_hash``; no
        filesystem access is required.
        """
        wu = self.conn.execute(
            """
            SELECT id, repo_url, base_branch, base_commit, head_branch, head_commit,
                   prompt, narrative, qa_result, pushed_at
            FROM work_units WHERE id = ?
            """,
            (work_unit_id,),
        ).fetchone()
        if wu is None:
            raise ValueError(f"Work unit not found: {work_unit_id}")

        policy_snaps = self.conn.execute(
            """
            SELECT session_id, manifest_hash, manifest_text, provenance, captured_at
            FROM policy_snapshots
            WHERE work_unit_id = ?
            ORDER BY captured_at
            """,
            (work_unit_id,),
        ).fetchall()

        diffs_rows = self.conn.execute(
            """
            SELECT s.file_path,
                   bl.pre_hash      AS before_hash,
                   s.blob_hash      AS after_hash,
                   b_diff.content   AS diff_compressed
            FROM (
                SELECT file_path, MAX(s2.id) AS last_id
                FROM   snapshots s2
                JOIN   work_unit_sessions wus ON wus.session_id = s2.session_id
                WHERE  wus.work_unit_id = ?
                GROUP  BY s2.file_path
            ) latest
            JOIN   snapshots s    ON s.id = latest.last_id
            LEFT JOIN baselines bl   ON bl.session_id = s.session_id
                                    AND bl.file_path  = s.file_path
            LEFT JOIN blobs b_diff   ON b_diff.hash = s.diff_hash
            """,
            (work_unit_id,),
        ).fetchall()

        action_rows = self.conn.execute(
            """
            SELECT seq, ts, session_id, action_type,
                   tool_name, tool_input, tool_outcome, tool_reason,
                   approved_exception, exception_detail, output_hash,
                   file_path, before_hash, after_hash,
                   content_short, content_hash
            FROM actions
            WHERE work_unit_id = ?
            ORDER BY seq
            """,
            (work_unit_id,),
        ).fetchall()

        qa_result = json.loads(wu[8]) if wu[8] else None
        qa_passed = bool(qa_result and qa_result.get("passed"))

        diffs = []
        for row in diffs_rows:
            diff_text = None
            if row[3]:
                try:
                    diff_text = zlib.decompress(row[3]).decode("utf-8", errors="replace")
                except Exception:
                    pass
            diffs.append(
                {
                    "file_path": row[0],
                    "before_hash": row[1],
                    "after_hash": row[2],
                    "diff": diff_text,
                }
            )

        formatted_actions = []
        for row in action_rows:
            a: dict = {
                "seq": row[0],
                "ts": row[1],
                "type": row[3],
            }
            if row[4]:  # tool_name
                a["tool"] = row[4]
            if row[5]:
                try:
                    a["input"] = json.loads(row[5])
                except Exception:
                    a["input"] = row[5]
            if row[6]:
                a["outcome"] = row[6]
            if row[7]:
                a["reason"] = row[7]
            if row[8]:
                a["approved_exception"] = bool(row[8])
            if row[9]:
                a["exception_detail"] = row[9]
            if row[11]:
                a["file_path"] = row[11]
            if row[12]:
                a["before_hash"] = row[12]
            if row[13]:
                a["after_hash"] = row[13]
            if row[14]:
                a["content"] = row[14]
            formatted_actions.append(a)

        return {
            "invocation": {
                "method": "query",
                "trigger": {
                    "source": "user_api",
                    "requester_system_uri": None,
                    "authenticated": True,
                },
                "input": {
                    "prompt": wu[6],
                    "policy_snapshots": [
                        {
                            "session_id": ps[0],
                            "manifest_hash": ps[1],
                            "manifest_text": ps[2],
                            "provenance": ps[3],
                            "captured_at": ps[4],
                        }
                        for ps in policy_snaps
                    ],
                    "git": {
                        "repo_url": wu[1],
                        "base_branch": wu[2],
                        "base_commit": wu[3],
                        "head_branch": wu[4],
                        "head_commit": wu[5],
                    },
                },
            },
            "outcome": {
                "result": {
                    "response": wu[7],
                    "diffs": diffs,
                    "actions": formatted_actions,
                    "qa": qa_result,
                },
                "status": "success" if qa_passed else "failed",
                "error": None,
            },
        }

    # ------------------------------------------------------------------
    # ATP cryptographic hashing
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_json(obj: object) -> bytes:
        """Deterministic JSON serialization suitable for hashing.

        Uses sorted keys and minimal separators so the same logical object
        always produces the same byte sequence regardless of insertion order.
        """
        # Match Go's json.Marshal: UTF-8 output, sorted keys, compact separators,
        # and HTML-safe escaping of <, >, & (Go does this by default).
        raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        raw = raw.replace("<", r"\u003c").replace(">", r"\u003e").replace("&", r"\u0026")
        return raw.encode("utf-8")

    def compute_invocation_hash(self, invocation: dict) -> str:
        """SHA-256 of the canonical invocation object.

        Per ATP spec, *invocation.input* must include ``mAInlined_policy_ref``
        and ``mAInlined_policy_version`` (read from the policy snapshot) so
        any policy change between sessions produces a different hash.
        """
        return "sha256:" + hashlib.sha256(self._canonical_json(invocation)).hexdigest()

    def compute_outcome_hash(self, outcome: dict) -> str:
        """SHA-256 of the canonical outcome object."""
        return "sha256:" + hashlib.sha256(self._canonical_json(outcome)).hexdigest()

    def compute_dependencies_hash(self, dependencies: list) -> str:
        """SHA-256 of the canonical dependencies array."""
        return "sha256:" + hashlib.sha256(self._canonical_json(dependencies)).hexdigest()

    # ------------------------------------------------------------------
    # ATP Full Proof and Proof Sketch assembly
    # ------------------------------------------------------------------

    def assemble_proof(self, work_unit_id: str) -> dict:
        """Assemble the ATP Full Proof JSON for *work_unit_id*.

        Wraps :meth:`assemble_payload` with the ATP envelope fields
        (``atp_metadata``, ``dependencies``, ``cryptography``, ``timestamp``,
        ``storage``) required by the depot submission API.

        ``invocation.input`` is extended with ``mAInlined_policy_ref`` and
        ``mAInlined_policy_version`` from the latest policy snapshot so the
        three hashes cryptographically bind the mAInlined policy version.
        """
        payload = self.assemble_payload(work_unit_id)

        # Fetch the latest policy snapshot to get policy_ref/version and
        # manifest-derived system_uri for atp_metadata.
        ps_row = self.conn.execute(
            """
            SELECT manifest_text, policy_ref, policy_version
            FROM   policy_snapshots
            WHERE  work_unit_id = ?
            ORDER  BY captured_at DESC
            LIMIT  1
            """,
            (work_unit_id,),
        ).fetchone()

        policy_ref = ""
        policy_version = ""
        system_uri = "contained://unknown/unknown"
        if ps_row:
            policy_ref = ps_row[1] or ""
            policy_version = ps_row[2] or ""
            try:
                manifest_data = yaml.safe_load(ps_row[0]) or {}
                mainlined = manifest_data.get("mainlined") or {}
                url = mainlined.get("url") or ""
                # Derive system_uri from mainlined URL: contained://<org>/<scope>
                # URL path is expected to end in /<org>/<scope>
                parts = [p for p in url.rstrip("/").split("/") if p]
                if len(parts) >= 2:
                    system_uri = f"contained://{parts[-2]}/{parts[-1]}"
            except Exception:
                pass

        wu_row = self.conn.execute(
            "SELECT ended_at FROM tasks WHERE session_id IN "
            "(SELECT session_id FROM work_unit_sessions WHERE work_unit_id = ?) "
            "ORDER BY ended_at DESC LIMIT 1",
            (work_unit_id,),
        ).fetchone()
        ended_at_ms = wu_row[0] if wu_row and wu_row[0] else int(time.time() * 1000)
        ended_at_iso = datetime.fromtimestamp(ended_at_ms / 1000, tz=timezone.utc).isoformat()
        expires_iso = datetime.fromtimestamp(
            ended_at_ms / 1000 + 90 * 86400, tz=timezone.utc
        ).isoformat()

        # Inject policy binding into invocation.input before hashing.
        invocation = payload["invocation"]
        invocation["input"]["mAInlined_policy_ref"] = policy_ref
        invocation["input"]["mAInlined_policy_version"] = policy_version

        outcome = payload["outcome"]
        prompt = invocation["input"].get("prompt") or ""

        # Claude API is an undeclared dependency until an ATP-compliant wrapper exists.
        dependencies: list = []

        return {
            "atp_metadata": {
                "spec_version": "0.1.0",
                "spec_uri": "https://atp.aiquilibria.com/spec/v0.1.0",
                "system_uri": system_uri,
                "system_type": "agent",
                "task_id": work_unit_id,
                "classification": {
                    "description": prompt[:200],
                    "ontology": {
                        "ontology_uri": "https://atp.aiquilibria.com/ontology/v0.1.0",
                        "occupation": "15-1252.00",
                        "work_activities": ["4.A.3.b.1"],
                        "capabilities": ["code-generation", "code-debugging"],
                    },
                },
            },
            "invocation": invocation,
            "outcome": outcome,
            "dependencies": dependencies,
            "cryptography": {
                "algorithm": "SHA-256",
                "invocation_hash": self.compute_invocation_hash(invocation),
                "outcome_hash": self.compute_outcome_hash(outcome),
                "dependencies_hash": self.compute_dependencies_hash(dependencies),
            },
            "timestamp": ended_at_iso,
            "storage": {
                "created_at": ended_at_iso,
                "ttl_days": 90,
                "expires_at": expires_iso,
            },
        }

    def assemble_proof_sketch(self, work_unit_id: str) -> dict:
        """Assemble the ATP Proof Sketch for Exchange commitment.

        Contains ``atp_metadata``, ``dependencies``, and ``cryptography``
        (the three hashes) only — no prompt, response, or file content.
        A challenger recomputes the hashes from the full proof and compares
        against the sketch to detect any tampering.
        """
        proof = self.assemble_proof(work_unit_id)
        return {
            "atp_metadata": proof["atp_metadata"],
            "dependencies": proof["dependencies"],
            "cryptography": proof["cryptography"],
            "timestamp": proof["timestamp"],
        }

    def set_task_transcript_path(self, session_id: str, transcript_path: str) -> None:
        """Store *transcript_path* on the task row for *session_id*."""
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET transcript_path = ? WHERE session_id = ?",
                (transcript_path, session_id),
            )

    def get_child_sessions(self, session_id: str) -> list[str]:
        """Return session_ids of all direct children of *session_id*."""
        rows = self.conn.execute(
            "SELECT session_id FROM tasks WHERE parent_session_id = ?",
            (session_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_open_root_tasks(self, older_than_secs: int = 3600) -> list[dict]:
        """
        Return root tasks in ``open`` state that started more than
        *older_than_secs* seconds ago, newest first.

        These represent sessions that were interrupted (e.g. REPL crash) before
        the agent could finish and transition to ``closed``.

        A root task has ``parent_session_id IS NULL``.
        """
        cutoff_ms = int((time.time() - older_than_secs) * 1000)
        rows = self.conn.execute(
            """
            SELECT session_id, prompt, started_at
            FROM tasks
            WHERE status = 'open'
              AND parent_session_id IS NULL
              AND started_at < ?
            ORDER BY started_at DESC
            """,
            (cutoff_ms,),
        ).fetchall()
        return [
            {
                "session_id": r[0],
                "prompt": r[1],
                "started_at": r[2],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Agent-tree traversal
    # ------------------------------------------------------------------

    def tree_session_ids(self, root_session_id: str) -> list[str]:
        """
        Return every session_id in the agent tree rooted at *root_session_id*
        (including the root itself), via recursive CTE.

        Works at any nesting depth.  Returns an empty list only if the root
        session does not exist in the tasks table.
        """
        rows = self.conn.execute(
            """
            WITH RECURSIVE tree(sid) AS (
                SELECT ? AS sid
                UNION ALL
                SELECT t.session_id
                FROM tasks t
                JOIN tree ON t.parent_session_id = tree.sid
            )
            SELECT sid FROM tree
            """,
            (root_session_id,),
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Per-file diff across the whole agent tree
    # ------------------------------------------------------------------

    def list_touched_files(self, root_session_id: str) -> list[str]:
        """
        Return distinct file paths touched by the agent tree rooted at
        *root_session_id*, sorted alphabetically.
        """
        session_ids = self.tree_session_ids(root_session_id)
        if not session_ids:
            return []
        placeholders = ",".join("?" * len(session_ids))
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT file_path FROM snapshots
            WHERE session_id IN ({placeholders})
            ORDER BY file_path
            """,
            session_ids,
        ).fetchall()
        return [r[0] for r in rows]

    def diff_task(self, root_session_id: str, file_path: str) -> str:
        """
        Compute a unified diff for *file_path* across the whole agent tree.

        Uses the earliest captured baseline for the file (MIN captured_at)
        across all sessions in the tree as the "before" state, and the most
        recently written snapshot as the "after" state.

        Returns an empty string if the file was not touched by this task tree
        or if no snapshot exists for it.
        """
        session_ids = self.tree_session_ids(root_session_id)
        if not session_ids:
            return ""

        placeholders = ",".join("?" * len(session_ids))

        # Earliest baseline for this file across the whole tree.
        baseline_row = self.conn.execute(
            f"""
            SELECT pre_hash FROM baselines
            WHERE file_path = ? AND session_id IN ({placeholders})
            ORDER BY captured_at ASC LIMIT 1
            """,
            [file_path, *session_ids],
        ).fetchone()

        if baseline_row is None:
            return ""  # file not touched by this task tree

        pre_hash = baseline_row[0]  # may be None (new file)

        # Most recent snapshot for this file across the whole tree.
        # Use s.id DESC as a tiebreaker when written_at timestamps collide
        # (possible within a single millisecond on fast machines).
        final_row = self.conn.execute(
            f"""
            SELECT b.content FROM snapshots s
            JOIN blobs b ON s.blob_hash = b.hash
            WHERE s.file_path = ? AND s.session_id IN ({placeholders})
            ORDER BY s.written_at DESC, s.id DESC LIMIT 1
            """,
            [file_path, *session_ids],
        ).fetchone()

        if final_row is None:
            return ""

        # Decode "before" lines.
        if pre_hash:
            raw_before = zlib.decompress(
                self.conn.execute(
                    "SELECT content FROM blobs WHERE hash = ?", (pre_hash,)
                ).fetchone()[0]
            )
            before_lines = raw_before.decode("utf-8", errors="replace").splitlines()
        else:
            before_lines = []  # new file

        # Decode "after" lines.
        after_lines = zlib.decompress(final_row[0]).decode("utf-8", errors="replace").splitlines()

        return "\n".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
                lineterm="",
            )
        )

    # ------------------------------------------------------------------
    # Audit query
    # ------------------------------------------------------------------

    def recent_audit_events(
        self,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """
        Return recent audit events, newest first.

        If *session_id* is given, scopes the query to the entire agent tree
        rooted at that session (not just the single session row).
        """
        if session_id:
            session_ids = self.tree_session_ids(session_id)
            placeholders = ",".join("?" * len(session_ids))
            rows = self.conn.execute(
                f"""
                SELECT ts, session_id, tool, input, outcome, reason
                FROM audit_events
                WHERE session_id IN ({placeholders})
                ORDER BY id DESC LIMIT ?
                """,
                [*session_ids, limit],
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT ts, session_id, tool, input, outcome, reason
                FROM audit_events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "ts": r[0],
                "session_id": r[1],
                "tool": r[2],
                "input": json.loads(r[3]) if r[3] else None,
                "outcome": r[4],
                "reason": r[5],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Garbage collection
    # ------------------------------------------------------------------

    def gc(self, keep_days: int = 14) -> None:
        """
        Prune old data.  Rules:

        - Tasks in ``open`` state (and their whole subtree) are **never** pruned.
        - Snapshots and baselines for closed tasks older than *keep_days* days
          are removed.
        - Blobs no longer referenced by any remaining snapshot or baseline
          are removed (orphaned blob sweep).
        - Audit events older than *keep_days* are removed, but at least the
          10,000 most recent events are always kept.
        - Task rows for closed sessions older than *keep_days* are removed last
          (after their snapshots/baselines are gone).
        """
        cutoff_ms = int((time.time() - keep_days * 86400) * 1000)

        # Collect every session_id that must be protected (open tasks and their
        # entire subtrees).
        protected = {
            row[0]
            for row in self.conn.execute(
                """
                WITH RECURSIVE tree(sid) AS (
                    SELECT session_id FROM tasks
                    WHERE status = 'open'
                    UNION ALL
                    SELECT t.session_id FROM tasks t
                    JOIN tree ON t.parent_session_id = tree.sid
                )
                SELECT sid FROM tree
                """
            ).fetchall()
        }

        placeholders = ",".join("?" * len(protected)) if protected else "'__none__'"
        protected_list = list(protected)

        with self.conn:
            # Remove old snapshots for non-protected sessions.
            self.conn.execute(
                f"""
                DELETE FROM snapshots
                WHERE written_at < ? AND session_id NOT IN ({placeholders})
                """,
                [cutoff_ms, *protected_list],
            )

            # Remove old baselines for non-protected sessions.
            self.conn.execute(
                f"""
                DELETE FROM baselines
                WHERE captured_at < ? AND session_id NOT IN ({placeholders})
                """,
                [cutoff_ms, *protected_list],
            )

            # Orphaned blob sweep — remove blobs no longer referenced by
            # any snapshot or baseline.
            self.conn.execute(
                """
                DELETE FROM blobs WHERE hash NOT IN (
                    SELECT DISTINCT blob_hash FROM snapshots
                    UNION
                    SELECT pre_hash FROM baselines WHERE pre_hash IS NOT NULL
                )
                """
            )

            # Audit event prune: remove old events from non-protected sessions,
            # but always keep the 10,000 most recent audit events globally.
            self.conn.execute(
                f"""
                DELETE FROM audit_events
                WHERE id IN (
                    SELECT id FROM audit_events
                    WHERE session_id NOT IN ({placeholders})
                    ORDER BY id DESC
                    LIMIT -1 OFFSET 10000
                )
                """,
                protected_list,
            )

            # Remove old task rows last (FK integrity: snapshots/baselines gone).
            self.conn.execute(
                f"""
                DELETE FROM tasks
                WHERE status = 'closed'
                  AND ended_at < ?
                  AND session_id NOT IN ({placeholders})
                """,
                [cutoff_ms, *protected_list],
            )
