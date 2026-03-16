"""
SlashTracer — continuous write tracking and task review.

Implements the core DB and write-capture API described in docs/slash-trace.md.

Phase 1: SlashTracer class with full schema, blob store, baselines, snapshots,
audit events, task lifecycle, tree diffing, and GC.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
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
-- One row per slash run invocation, REPL session, or sub-agent session.
CREATE TABLE IF NOT EXISTS tasks (
    session_id          TEXT    PRIMARY KEY,
    parent_session_id   TEXT    REFERENCES tasks(session_id),
    prompt              TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'open',  -- open|pending_review|closed|abandoned
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    summary             TEXT    -- JSON: per-file diff summary, populated at pending_review
);

CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- Per-session file baseline.
-- Captured by tracer_pre.py before the first Write/Edit/MultiEdit to a file in a session.
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

CREATE INDEX IF NOT EXISTS idx_snapshots_file    ON snapshots(file_path, written_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON snapshots(session_id, written_at DESC);

-- Audit event log (replaces .slash/audit/pipeline.jsonl).
-- One row per tool call (all tools, not just writes).
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,   -- ISO-8601 timestamp
    session_id  TEXT    REFERENCES tasks(session_id),
    tool        TEXT    NOT NULL,
    input       TEXT,               -- JSON: tool-specific trace unit (see log_event)
    outcome     TEXT    NOT NULL,   -- "success" | "denied"
    reason      TEXT                -- populated on denial
);

CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_tool    ON audit_events(tool, id DESC);
"""

# Tools whose content is fully captured in the blobs/snapshots tables.
# audit_events stores only the file path as a back-reference.
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Read-only tools: no trace unit worth storing.
_READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}


class SlashTracer:
    """
    Core write-tracking and task-review engine.

    Instantiate once per process with the path to `.slash/tracer.db`.
    The database is created (with WAL mode) if it does not exist.
    All public methods are safe to call from concurrent hook subprocesses:
    SQLite WAL serialises writers; INSERT OR IGNORE guards idempotent paths.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Create all tables and indexes if they do not already exist."""
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()

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
        row = self.conn.execute(
            "SELECT content FROM blobs WHERE hash = ?", (blob_hash,)
        ).fetchone()
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
                        (ts, session_id, tool, input, outcome, reason)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        session_id,
                        tool,
                        json.dumps(trace_unit) if trace_unit is not None else None,
                        outcome,
                        reason,
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
            # No diff value from read-only operations.
            return None

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
        Register a new task as ``open``.

        INSERT OR IGNORE is idempotent — calling this twice for the same
        session_id (e.g. REPL resume) leaves the existing row untouched.
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO tasks
                    (session_id, parent_session_id, prompt, status, started_at)
                VALUES (?, ?, ?, 'open', ?)
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
        """
        now_ms = int(time.time() * 1000)
        with self.conn:
            self.conn.execute(
                """
                UPDATE tasks
                SET status = ?, ended_at = ?, summary = ?
                WHERE session_id = ?
                """,
                (
                    status,
                    now_ms,
                    json.dumps(summary) if summary is not None else None,
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

    def get_pending_reviews(self) -> list[dict]:
        """
        Return all root tasks in ``pending_review`` state, newest first.

        A root task has ``parent_session_id IS NULL``.
        """
        rows = self.conn.execute(
            """
            SELECT session_id, prompt, started_at, summary
            FROM tasks
            WHERE status = 'pending_review'
              AND parent_session_id IS NULL
            ORDER BY started_at DESC
            """
        ).fetchall()
        return [
            {
                "session_id": r[0],
                "prompt": r[1],
                "started_at": r[2],
                "summary": json.loads(r[3]) if r[3] else None,
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
        after_lines = (
            zlib.decompress(final_row[0])
            .decode("utf-8", errors="replace")
            .splitlines()
        )

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

        - Tasks in ``open`` or ``pending_review`` (and their whole subtree)
          are **never** pruned.
        - Snapshots and baselines for closed/abandoned tasks older than
          *keep_days* days are removed.
        - Blobs no longer referenced by any remaining snapshot or baseline
          are removed (orphaned blob sweep).
        - Audit events older than *keep_days* are removed, but at least the
          10,000 most recent events are always kept.
        - Task rows for closed/abandoned sessions older than *keep_days* are
          removed last (after their snapshots/baselines are gone).
        """
        cutoff_ms = int((time.time() - keep_days * 86400) * 1000)

        # Collect every session_id that must be protected (open or pending_review,
        # and their entire subtrees).
        protected = {
            row[0]
            for row in self.conn.execute(
                """
                WITH RECURSIVE tree(sid) AS (
                    SELECT session_id FROM tasks
                    WHERE status IN ('open', 'pending_review')
                    UNION ALL
                    SELECT t.session_id FROM tasks t
                    JOIN tree ON t.parent_session_id = tree.sid
                )
                SELECT sid FROM tree
                """
            ).fetchall()
        }

        placeholders = (
            ",".join("?" * len(protected)) if protected else "'__none__'"
        )
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
                WHERE status IN ('closed', 'abandoned')
                  AND ended_at < ?
                  AND session_id NOT IN ({placeholders})
                """,
                [cutoff_ms, *protected_list],
            )
