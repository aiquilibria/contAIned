"""
contAInedTracer — continuous write tracking and task review.

Implements the core DB and write-capture API described in docs/contAIned-trace.md.

Phase 1: contAInedTracer class with full schema, blob store, baselines, snapshots,
audit events, task lifecycle, tree diffing, and GC.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
import threading as _threading
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
    narrative           TEXT    -- agent's final human-readable summary, set on close
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
    metadata    TEXT                -- Optional JSON: pass number, notes, etc.
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
            "ALTER TABLE tasks ADD COLUMN narrative TEXT",
            "ALTER TABLE audit_events ADD COLUMN approved_exception INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE audit_events ADD COLUMN exception_detail TEXT",
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
        Returns the blob hash.
        """
        blob_hash = self._store_blob(content)
        now_ms = int(time.time() * 1000)

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO snapshots
                    (session_id, file_path, blob_hash, written_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    file_path,
                    blob_hash,
                    now_ms,
                    json.dumps(metadata) if metadata else None,
                ),
            )

        return blob_hash

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
        narrative: Optional[str] = None,
    ) -> None:
        """
        Transition *session_id*'s task to *status*.

        Optionally attaches a diff summary (JSON-serialisable dict) to
        ``tasks.summary`` and a human-readable narrative to ``tasks.narrative``.
        Sets ``ended_at`` to now.

        ``COALESCE(?, col)`` semantics: passing ``None`` for *summary* or
        *narrative* preserves whatever value is already stored in the DB.
        Pass an explicit value to overwrite.
        """
        now_ms = int(time.time() * 1000)
        with self.conn:
            self.conn.execute(
                """
                UPDATE tasks
                SET status    = ?,
                    ended_at  = ?,
                    summary   = COALESCE(?, summary),
                    narrative = COALESCE(?, narrative)
                WHERE session_id = ?
                """,
                (
                    status,
                    now_ms,
                    json.dumps(summary) if summary is not None else None,
                    narrative,
                    session_id,
                ),
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
