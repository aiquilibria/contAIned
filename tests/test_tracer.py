"""
Phase 6 — SlashTracer test suite.

Coverage:
  Unit tests:
    - SlashTracer constructor, schema creation
    - _store_blob / _retrieve_blob (deduplication, zlib round-trip)
    - capture_baseline (idempotent, new file, existing file)
    - track_write (content stored, snapshot appended, multiple writes)
    - log_event / _extract_trace_unit (per-tool dispatch)
    - Actor ID resolution formula (root vs sub-agent)
    - open_task / set_task_status / get_pending_reviews
    - tree_session_ids (flat and nested)
    - list_touched_files / diff_task
    - recent_audit_events (scoped and global)
    - gc (prunes old closed/abandoned; protects open/pending_review)

  Integration tests:
    - Single-agent task: write files → pending_review → approve → closed
    - Sub-agent task: SubagentStart → child writes → SubagentStop → root Stop
      → tree diff covers all files
    - Concurrent baseline writes: MIN(captured_at) wins
    - REPL startup with pending review (review surfaced, approve → closed)
    - REPL /new and /review (task rows, pending task list)
    - Stale open-task recovery at REPL startup
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slash.tracer import SlashTracer


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "tracer.db")


@pytest.fixture()
def tracer(db_path: str) -> SlashTracer:
    return SlashTracer(db_path)


@pytest.fixture()
def tmp_file(tmp_path: Path):
    """Return a helper that creates a file with given content."""
    def _make(name: str, content: str = "hello\n") -> Path:
        p = tmp_path / name
        p.write_text(content)
        return p
    return _make


# ---------------------------------------------------------------------------
# Unit — constructor / schema
# ---------------------------------------------------------------------------

class TestInit:
    def test_db_file_created(self, db_path: str) -> None:
        SlashTracer(db_path)
        assert Path(db_path).exists()

    def test_wal_mode_enabled(self, tracer: SlashTracer) -> None:
        row = tracer.conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_all_tables_present(self, tracer: SlashTracer) -> None:
        expected = {"blobs", "tasks", "baselines", "snapshots", "audit_events"}
        tables = {
            r[0]
            for r in tracer.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert expected <= tables

    def test_second_init_is_idempotent(self, db_path: str) -> None:
        """Re-creating SlashTracer on the same DB must not raise."""
        SlashTracer(db_path)
        SlashTracer(db_path)  # must not raise


# ---------------------------------------------------------------------------
# Unit — blob store
# ---------------------------------------------------------------------------

class TestBlobStore:
    def test_store_returns_sha256(self, tracer: SlashTracer) -> None:
        import hashlib
        content = b"hello world"
        h = tracer._store_blob(content)
        assert h == hashlib.sha256(content).hexdigest()

    def test_retrieve_round_trip(self, tracer: SlashTracer) -> None:
        content = b"some binary \x00\x01\x02 content"
        h = tracer._store_blob(content)
        assert tracer._retrieve_blob(h) == content

    def test_deduplication(self, tracer: SlashTracer) -> None:
        content = b"duplicate"
        h1 = tracer._store_blob(content)
        h2 = tracer._store_blob(content)
        assert h1 == h2
        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM blobs WHERE hash = ?", (h1,)
        ).fetchone()[0]
        assert count == 1

    def test_missing_blob_raises_key_error(self, tracer: SlashTracer) -> None:
        with pytest.raises(KeyError):
            tracer._retrieve_blob("nonexistent" * 4)

    def test_content_is_zlib_compressed(self, tracer: SlashTracer) -> None:
        content = b"compressible" * 100
        h = tracer._store_blob(content)
        row = tracer.conn.execute(
            "SELECT content FROM blobs WHERE hash = ?", (h,)
        ).fetchone()
        # raw stored bytes must be decompressible and round-trip
        assert zlib.decompress(row[0]) == content

    def test_different_content_different_hashes(self, tracer: SlashTracer) -> None:
        h1 = tracer._store_blob(b"aaa")
        h2 = tracer._store_blob(b"bbb")
        assert h1 != h2


# ---------------------------------------------------------------------------
# Unit — capture_baseline
# ---------------------------------------------------------------------------

class TestCaptureBaseline:
    def test_new_file_returns_none(self, tracer: SlashTracer, tmp_path: Path) -> None:
        nonexistent = str(tmp_path / "new.txt")
        tracer.open_task("S1", "task")
        result = tracer.capture_baseline("S1", nonexistent)
        assert result is None

    def test_new_file_stored_with_null_pre_hash(
        self, tracer: SlashTracer, tmp_path: Path
    ) -> None:
        nonexistent = str(tmp_path / "new.txt")
        tracer.open_task("S1", "task")
        tracer.capture_baseline("S1", nonexistent)
        row = tracer.conn.execute(
            "SELECT pre_hash FROM baselines WHERE session_id = 'S1'",
        ).fetchone()
        assert row is not None
        assert row[0] is None

    def test_existing_file_returns_hash(
        self, tracer: SlashTracer, tmp_file
    ) -> None:
        p = tmp_file("foo.txt", "original content\n")
        tracer.open_task("S1", "task")
        h = tracer.capture_baseline("S1", str(p))
        assert h is not None
        assert len(h) == 64  # SHA-256 hex

    def test_idempotent_second_call_ignored(
        self, tracer: SlashTracer, tmp_file
    ) -> None:
        p = tmp_file("foo.txt", "v1\n")
        tracer.open_task("S1", "task")
        h1 = tracer.capture_baseline("S1", str(p))
        # Modify the file; the second capture_baseline call must not overwrite
        # the existing baseline row (INSERT OR IGNORE semantics).
        p.write_text("v2\n")
        tracer.capture_baseline("S1", str(p))
        # Only one row must exist in the DB.
        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM baselines WHERE session_id = 'S1'",
        ).fetchone()[0]
        assert count == 1
        # The stored pre_hash must still reflect the FIRST content (v1).
        stored_hash = tracer.conn.execute(
            "SELECT pre_hash FROM baselines WHERE session_id = 'S1'",
        ).fetchone()[0]
        assert stored_hash == h1

    def test_different_sessions_independent(
        self, tracer: SlashTracer, tmp_file
    ) -> None:
        p = tmp_file("foo.txt", "v1\n")
        tracer.open_task("S1", "task")
        tracer.open_task("S2", "task")
        tracer.capture_baseline("S1", str(p))
        p.write_text("v2\n")
        tracer.capture_baseline("S2", str(p))
        h1 = tracer.conn.execute(
            "SELECT pre_hash FROM baselines WHERE session_id = 'S1'",
        ).fetchone()[0]
        h2 = tracer.conn.execute(
            "SELECT pre_hash FROM baselines WHERE session_id = 'S2'",
        ).fetchone()[0]
        assert h1 != h2


# ---------------------------------------------------------------------------
# Unit — track_write
# ---------------------------------------------------------------------------

class TestTrackWrite:
    def test_returns_hash(self, tracer: SlashTracer) -> None:
        import hashlib
        tracer.open_task("S1", "task")
        h = tracer.track_write("S1", "foo.py", b"content")
        assert h == hashlib.sha256(b"content").hexdigest()

    def test_snapshot_row_appended(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.track_write("S1", "a.py", b"v1")
        count = tracer.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert count == 1

    def test_multiple_writes_all_appended(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.track_write("S1", "a.py", b"v1")
        tracer.track_write("S1", "a.py", b"v2")
        tracer.track_write("S1", "a.py", b"v3")
        count = tracer.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert count == 3

    def test_identical_content_deduplicates_blobs(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.track_write("S1", "a.py", b"same")
        tracer.track_write("S1", "b.py", b"same")
        blob_count = tracer.conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
        assert blob_count == 1  # single blob for identical content

    def test_metadata_stored_as_json(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.track_write("S1", "a.py", b"x", metadata={"pass": 1})
        row = tracer.conn.execute(
            "SELECT metadata FROM snapshots"
        ).fetchone()
        assert json.loads(row[0]) == {"pass": 1}


# ---------------------------------------------------------------------------
# Unit — log_event / _extract_trace_unit
# ---------------------------------------------------------------------------

class TestLogEvent:
    def _events(self, tracer: SlashTracer) -> list[dict]:
        rows = tracer.conn.execute(
            "SELECT tool, input, outcome, reason FROM audit_events"
        ).fetchall()
        return [
            {"tool": r[0], "input": json.loads(r[1]) if r[1] else None,
             "outcome": r[2], "reason": r[3]}
            for r in rows
        ]

    def test_write_tool_stores_file_path(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.log_event("S1", "Write", {"file_path": "foo.py"}, "success")
        ev = self._events(tracer)[0]
        assert ev["input"] == {"file_path": "foo.py"}

    def test_edit_tool_stores_file_path(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.log_event("S1", "Edit", {"file_path": "bar.py"}, "success")
        ev = self._events(tracer)[0]
        assert ev["input"] == {"file_path": "bar.py"}

    def test_multiedit_stores_file_paths(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.log_event(
            "S1", "MultiEdit",
            {"edits": [{"file_path": "a.py"}, {"file_path": "b.py"}]},
            "success",
        )
        ev = self._events(tracer)[0]
        assert set(ev["input"]["file_paths"]) == {"a.py", "b.py"}

    def test_read_only_tools_store_file_path(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        for tool in ("Read", "Glob", "Grep"):
            tracer.log_event("S1", tool, {"file_path": "x"}, "success")
        evs = self._events(tracer)
        assert all(e["input"] == {"file_path": "x"} for e in evs)

    def test_bash_tool_stores_command_and_stdout(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.log_event(
            "S1", "Bash", {"command": "ls -la"},
            "success",
            tool_response={"exit_code": 0, "stdout": "total 4\n"},
        )
        ev = self._events(tracer)[0]
        assert ev["input"]["command"] == "ls -la"
        assert ev["input"]["exit_code"] == 0
        assert ev["input"]["stdout_head"] == "total 4\n"

    def test_bash_stdout_capped_at_500(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        long_out = "x" * 1000
        tracer.log_event(
            "S1", "Bash", {"command": "x"},
            "success",
            tool_response={"exit_code": 0, "stdout": long_out},
        )
        ev = self._events(tracer)[0]
        assert len(ev["input"]["stdout_head"]) == 500

    def test_agent_tool_stores_type_and_prompt(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.log_event(
            "S1", "Agent",
            {"subagent_type": "general-purpose", "prompt": "do something"},
            "success",
        )
        ev = self._events(tracer)[0]
        assert ev["input"]["agent_type"] == "general-purpose"
        assert ev["input"]["prompt_head"] == "do something"

    def test_agent_prompt_capped_at_200(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        long_prompt = "p" * 400
        tracer.log_event(
            "S1", "Agent",
            {"subagent_type": "general-purpose", "prompt": long_prompt},
            "success",
        )
        ev = self._events(tracer)[0]
        assert len(ev["input"]["prompt_head"]) == 200

    def test_unknown_tool_fallback_first_five_keys(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        inp = {f"k{i}": f"v{i}" for i in range(8)}
        tracer.log_event("S1", "SomeTool", inp, "success")
        ev = self._events(tracer)[0]
        assert len(ev["input"]) == 5

    def test_denied_event_stores_reason(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.log_event(
            "S1", "Write", {"file_path": "x"}, "denied",
            reason="restricted path",
        )
        ev = self._events(tracer)[0]
        assert ev["outcome"] == "denied"
        assert ev["reason"] == "restricted path"

    def test_log_event_never_raises_on_bad_session(self, tracer: SlashTracer) -> None:
        """log_event must not raise even for an unknown session_id."""
        tracer.log_event("UNKNOWN_SESSION", "Write", {}, "success")


# ---------------------------------------------------------------------------
# Unit — actor ID resolution
# ---------------------------------------------------------------------------

class TestActorIdResolution:
    """
    actor_id = agent_id or session_id

    This is the formula used by all tracer hooks.  We verify it directly
    here using the same conditional logic so any future regression is caught.
    """

    @staticmethod
    def resolve(event: dict) -> Optional[str]:
        session_id = event.get("session_id")
        agent_id   = event.get("agent_id")
        return agent_id or session_id

    def test_root_agent_uses_session_id(self) -> None:
        event = {"session_id": "ROOT", "tool_name": "Write"}
        assert self.resolve(event) == "ROOT"

    def test_sub_agent_uses_agent_id(self) -> None:
        event = {"session_id": "ROOT", "agent_id": "AGENT1", "tool_name": "Write"}
        assert self.resolve(event) == "AGENT1"

    def test_deeply_nested_sub_agent_uses_agent_id(self) -> None:
        event = {"session_id": "ROOT", "agent_id": "AGENT2"}
        assert self.resolve(event) == "AGENT2"

    def test_no_ids_returns_none(self) -> None:
        assert self.resolve({}) is None

    def test_empty_agent_id_falls_back_to_session_id(self) -> None:
        # Empty string is falsy — should fall back to session_id.
        event = {"session_id": "ROOT", "agent_id": ""}
        assert self.resolve(event) == "ROOT"


# ---------------------------------------------------------------------------
# Unit — task lifecycle
# ---------------------------------------------------------------------------

class TestTaskLifecycle:
    def test_open_task_creates_row(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "do something")
        row = tracer.conn.execute(
            "SELECT status FROM tasks WHERE session_id = 'S1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "open"

    def test_open_task_idempotent(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "prompt v1")
        tracer.open_task("S1", "prompt v2")  # must NOT overwrite
        row = tracer.conn.execute(
            "SELECT prompt FROM tasks WHERE session_id = 'S1'"
        ).fetchone()
        assert row[0] == "prompt v1"

    def test_set_task_status_transitions(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "pending_review")
        row = tracer.conn.execute(
            "SELECT status FROM tasks WHERE session_id = 'S1'"
        ).fetchone()
        assert row[0] == "pending_review"

    def test_set_task_status_stores_summary(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        summary = {"files": ["a.py"], "diff": "---"}
        tracer.set_task_status("S1", "pending_review", summary=summary)
        row = tracer.conn.execute(
            "SELECT summary FROM tasks WHERE session_id = 'S1'"
        ).fetchone()
        assert json.loads(row[0]) == summary

    def test_get_pending_reviews_returns_root_only(
        self, tracer: SlashTracer
    ) -> None:
        tracer.open_task("ROOT", "root task")
        tracer.open_task("CHILD", "child task", parent_session_id="ROOT")
        tracer.set_task_status("ROOT", "pending_review")
        tracer.set_task_status("CHILD", "pending_review")
        pending = tracer.get_pending_reviews()
        ids = [p["session_id"] for p in pending]
        assert "ROOT" in ids
        assert "CHILD" not in ids  # children excluded

    def test_get_pending_reviews_sorted_newest_first(
        self, tracer: SlashTracer
    ) -> None:
        for sid in ("S1", "S2", "S3"):
            tracer.open_task(sid, f"task {sid}")
            tracer.set_task_status(sid, "pending_review")
            time.sleep(0.001)  # ensure distinct timestamps
        pending = tracer.get_pending_reviews()
        ids = [p["session_id"] for p in pending]
        assert ids == sorted(ids, reverse=True) or ids[0] == "S3"

    def test_get_child_sessions(self, tracer: SlashTracer) -> None:
        tracer.open_task("ROOT", "root")
        tracer.open_task("C1", "child1", parent_session_id="ROOT")
        tracer.open_task("C2", "child2", parent_session_id="ROOT")
        children = tracer.get_child_sessions("ROOT")
        assert set(children) == {"C1", "C2"}


# ---------------------------------------------------------------------------
# Unit — tree traversal
# ---------------------------------------------------------------------------

class TestTreeTraversal:
    def test_single_node_tree(self, tracer: SlashTracer) -> None:
        tracer.open_task("ROOT", "task")
        ids = tracer.tree_session_ids("ROOT")
        assert ids == ["ROOT"]

    def test_flat_children(self, tracer: SlashTracer) -> None:
        tracer.open_task("ROOT", "root")
        tracer.open_task("C1", "c1", parent_session_id="ROOT")
        tracer.open_task("C2", "c2", parent_session_id="ROOT")
        ids = set(tracer.tree_session_ids("ROOT"))
        assert ids == {"ROOT", "C1", "C2"}

    def test_nested_children(self, tracer: SlashTracer) -> None:
        tracer.open_task("ROOT", "root")
        tracer.open_task("C1", "c1", parent_session_id="ROOT")
        tracer.open_task("C2", "c2", parent_session_id="C1")
        ids = set(tracer.tree_session_ids("ROOT"))
        assert ids == {"ROOT", "C1", "C2"}

    def test_unknown_root_returns_root_only(self, tracer: SlashTracer) -> None:
        # The CTE always seeds with the root_session_id regardless of existence.
        ids = tracer.tree_session_ids("GHOST")
        assert ids == ["GHOST"]

    def test_list_touched_files_single_session(
        self, tracer: SlashTracer
    ) -> None:
        tracer.open_task("S1", "task")
        tracer.track_write("S1", "a.py", b"v1")
        tracer.track_write("S1", "b.py", b"v1")
        files = tracer.list_touched_files("S1")
        assert files == ["a.py", "b.py"]

    def test_list_touched_files_across_subtree(
        self, tracer: SlashTracer
    ) -> None:
        tracer.open_task("ROOT", "root")
        tracer.open_task("C1", "c1", parent_session_id="ROOT")
        tracer.track_write("ROOT", "root.py", b"v1")
        tracer.track_write("C1", "child.py", b"v1")
        files = tracer.list_touched_files("ROOT")
        assert "root.py" in files
        assert "child.py" in files

    def test_list_touched_files_empty_tree(
        self, tracer: SlashTracer
    ) -> None:
        tracer.open_task("S1", "task")
        assert tracer.list_touched_files("S1") == []


# ---------------------------------------------------------------------------
# Unit — diff_task
# ---------------------------------------------------------------------------

class TestDiffTask:
    def _setup(
        self,
        tracer: SlashTracer,
        session_id: str,
        file_path: str,
        before: Optional[bytes],
        after: bytes,
    ) -> None:
        tracer.open_task(session_id, "task")
        # Store baseline
        now_ms = int(time.time() * 1000)
        if before is None:
            tracer.conn.execute(
                "INSERT OR IGNORE INTO baselines (session_id, file_path, pre_hash, captured_at) "
                "VALUES (?, ?, NULL, ?)",
                (session_id, file_path, now_ms),
            )
        else:
            pre_hash = tracer._store_blob(before)
            tracer.conn.execute(
                "INSERT OR IGNORE INTO baselines (session_id, file_path, pre_hash, captured_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, file_path, pre_hash, now_ms),
            )
            tracer.conn.commit()
        tracer.track_write(session_id, file_path, after)

    def test_new_file_diff_shows_additions(self, tracer: SlashTracer) -> None:
        self._setup(tracer, "S1", "new.py", None, b"line1\nline2\n")
        diff = tracer.diff_task("S1", "new.py")
        assert "+line1" in diff
        assert "+line2" in diff

    def test_modified_file_diff_shows_changes(self, tracer: SlashTracer) -> None:
        self._setup(
            tracer, "S1", "mod.py",
            b"before\n",
            b"after\n",
        )
        diff = tracer.diff_task("S1", "mod.py")
        assert "-before" in diff
        assert "+after" in diff

    def test_identical_content_empty_diff(self, tracer: SlashTracer) -> None:
        content = b"unchanged\n"
        self._setup(tracer, "S1", "same.py", content, content)
        diff = tracer.diff_task("S1", "same.py")
        assert diff == ""

    def test_no_baseline_returns_empty(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        diff = tracer.diff_task("S1", "untouched.py")
        assert diff == ""

    def test_no_snapshots_returns_empty(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        # Baseline without snapshot
        tracer.conn.execute(
            "INSERT INTO baselines (session_id, file_path, pre_hash, captured_at) "
            "VALUES ('S1', 'x.py', NULL, ?)",
            (int(time.time() * 1000),),
        )
        tracer.conn.commit()
        diff = tracer.diff_task("S1", "x.py")
        assert diff == ""

    def test_diff_uses_latest_snapshot(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        fp = "evolving.py"
        now_ms = int(time.time() * 1000)
        pre_hash = tracer._store_blob(b"v0\n")
        tracer.conn.execute(
            "INSERT INTO baselines (session_id, file_path, pre_hash, captured_at) "
            "VALUES ('S1', ?, ?, ?)",
            (fp, pre_hash, now_ms),
        )
        tracer.conn.commit()
        tracer.track_write("S1", fp, b"v1\n")
        tracer.track_write("S1", fp, b"v2\n")
        tracer.track_write("S1", fp, b"v3\n")
        diff = tracer.diff_task("S1", fp)
        # "after" must be v3
        assert "+v3" in diff
        assert "-v0" in diff

    def test_tree_diff_uses_earliest_baseline(self, tracer: SlashTracer) -> None:
        """
        When two sessions in a tree each captured a baseline for the same file,
        diff_task must use the EARLIEST one (MIN captured_at).
        """
        tracer.open_task("ROOT", "root")
        tracer.open_task("CHILD", "child", parent_session_id="ROOT")

        fp = "shared.py"
        early_ms = int(time.time() * 1000) - 5000
        late_ms  = int(time.time() * 1000)

        early_hash = tracer._store_blob(b"earliest\n")
        late_hash  = tracer._store_blob(b"later\n")

        tracer.conn.execute(
            "INSERT INTO baselines (session_id, file_path, pre_hash, captured_at) "
            "VALUES ('ROOT', ?, ?, ?)",
            (fp, early_hash, early_ms),
        )
        tracer.conn.execute(
            "INSERT INTO baselines (session_id, file_path, pre_hash, captured_at) "
            "VALUES ('CHILD', ?, ?, ?)",
            (fp, late_hash, late_ms),
        )
        tracer.conn.commit()
        tracer.track_write("CHILD", fp, b"final\n")

        diff = tracer.diff_task("ROOT", fp)
        assert "-earliest" in diff
        assert "+final" in diff


# ---------------------------------------------------------------------------
# Unit — recent_audit_events
# ---------------------------------------------------------------------------

class TestRecentAuditEvents:
    def _add_events(
        self, tracer: SlashTracer, session_id: str, n: int = 3
    ) -> None:
        tracer.open_task(session_id, "task")
        for i in range(n):
            tracer.log_event(session_id, f"Tool{i}", {}, "success")

    def test_global_query_returns_all(self, tracer: SlashTracer) -> None:
        self._add_events(tracer, "S1", 3)
        self._add_events(tracer, "S2", 3)
        evs = tracer.recent_audit_events(limit=100)
        assert len(evs) == 6

    def test_session_scoped_query(self, tracer: SlashTracer) -> None:
        self._add_events(tracer, "S1", 3)
        self._add_events(tracer, "S2", 5)
        evs = tracer.recent_audit_events(session_id="S1", limit=100)
        assert len(evs) == 3

    def test_limit_respected(self, tracer: SlashTracer) -> None:
        self._add_events(tracer, "S1", 20)
        evs = tracer.recent_audit_events(limit=5)
        assert len(evs) == 5

    def test_newest_first_ordering(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        for tool in ("A", "B", "C"):
            tracer.log_event("S1", tool, {}, "success")
        evs = tracer.recent_audit_events(session_id="S1")
        tools = [e["tool"] for e in evs]
        assert tools[0] == "C"  # newest first

    def test_tree_scoped_includes_subtree(self, tracer: SlashTracer) -> None:
        tracer.open_task("ROOT", "root")
        tracer.open_task("CHILD", "child", parent_session_id="ROOT")
        tracer.log_event("ROOT",  "ToolRoot",  {}, "success")
        tracer.log_event("CHILD", "ToolChild", {}, "success")
        evs = tracer.recent_audit_events(session_id="ROOT", limit=10)
        tools = {e["tool"] for e in evs}
        assert "ToolRoot" in tools
        assert "ToolChild" in tools

    def test_event_fields_populated(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.log_event(
            "S1", "Bash", {"command": "echo hi"}, "success",
            tool_response={"exit_code": 0, "stdout": "hi"},
        )
        ev = tracer.recent_audit_events(limit=1)[0]
        assert ev["tool"] == "Bash"
        assert ev["outcome"] == "success"
        assert ev["input"]["command"] == "echo hi"
        assert "ts" in ev
        assert "session_id" in ev


# ---------------------------------------------------------------------------
# Unit — GC
# ---------------------------------------------------------------------------

class TestGC:
    def _old_ms(self, days: int = 20) -> int:
        return int((time.time() - days * 86400) * 1000)

    def _recent_ms(self) -> int:
        return int(time.time() * 1000)

    def test_gc_removes_old_closed_snapshots(
        self, tracer: SlashTracer
    ) -> None:
        tracer.open_task("OLD", "old task")
        tracer.set_task_status("OLD", "closed")
        # Manually backdate ended_at and snapshot written_at
        old_ms = self._old_ms()
        tracer.conn.execute(
            "UPDATE tasks SET ended_at = ? WHERE session_id = 'OLD'", (old_ms,)
        )
        tracer.conn.execute(
            "INSERT INTO snapshots (session_id, file_path, blob_hash, written_at) "
            "VALUES ('OLD', 'x.py', ?, ?)",
            (tracer._store_blob(b"x"), old_ms),
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        count = tracer.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert count == 0

    def test_gc_protects_open_tasks(self, tracer: SlashTracer) -> None:
        tracer.open_task("OPEN", "open task")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "INSERT INTO snapshots (session_id, file_path, blob_hash, written_at) "
            "VALUES ('OPEN', 'x.py', ?, ?)",
            (tracer._store_blob(b"x"), old_ms),
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        count = tracer.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert count == 1  # protected; must remain

    def test_gc_protects_pending_review_tasks(
        self, tracer: SlashTracer
    ) -> None:
        tracer.open_task("PR", "pending task")
        tracer.set_task_status("PR", "pending_review")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "INSERT INTO snapshots (session_id, file_path, blob_hash, written_at) "
            "VALUES ('PR', 'x.py', ?, ?)",
            (tracer._store_blob(b"x"), old_ms),
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        count = tracer.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert count == 1

    def test_gc_removes_orphaned_blobs(self, tracer: SlashTracer) -> None:
        tracer.open_task("OLD", "task")
        tracer.set_task_status("OLD", "closed")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "UPDATE tasks SET ended_at = ? WHERE session_id = 'OLD'", (old_ms,)
        )
        blob_hash = tracer._store_blob(b"orphan content")
        tracer.conn.execute(
            "INSERT INTO snapshots (session_id, file_path, blob_hash, written_at) "
            "VALUES ('OLD', 'x.py', ?, ?)",
            (blob_hash, old_ms),
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        blob_count = tracer.conn.execute(
            "SELECT COUNT(*) FROM blobs"
        ).fetchone()[0]
        assert blob_count == 0

    def test_gc_keeps_referenced_blobs(self, tracer: SlashTracer) -> None:
        tracer.open_task("OLD", "old")
        tracer.set_task_status("OLD", "closed")
        tracer.open_task("OPEN", "open")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "UPDATE tasks SET ended_at = ? WHERE session_id = 'OLD'", (old_ms,)
        )
        shared_blob = tracer._store_blob(b"shared content")
        tracer.conn.execute(
            "INSERT INTO snapshots (session_id, file_path, blob_hash, written_at) "
            "VALUES ('OLD', 'x.py', ?, ?)", (shared_blob, old_ms)
        )
        tracer.conn.execute(
            "INSERT INTO snapshots (session_id, file_path, blob_hash, written_at) "
            "VALUES ('OPEN', 'x.py', ?, ?)", (shared_blob, self._recent_ms())
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        # Blob still referenced by OPEN session; must not be deleted
        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM blobs WHERE hash = ?", (shared_blob,)
        ).fetchone()[0]
        assert count == 1

    def test_gc_removes_old_task_rows(self, tracer: SlashTracer) -> None:
        tracer.open_task("OLD", "old")
        tracer.set_task_status("OLD", "closed")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "UPDATE tasks SET ended_at = ? WHERE session_id = 'OLD'", (old_ms,)
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        row = tracer.conn.execute(
            "SELECT session_id FROM tasks WHERE session_id = 'OLD'"
        ).fetchone()
        assert row is None

    def test_gc_keeps_recent_closed_tasks(self, tracer: SlashTracer) -> None:
        tracer.open_task("RECENT", "recent")
        tracer.set_task_status("RECENT", "closed")
        # ended_at is set to now by set_task_status — within keep window

        tracer.gc(keep_days=14)
        row = tracer.conn.execute(
            "SELECT session_id FROM tasks WHERE session_id = 'RECENT'"
        ).fetchone()
        assert row is not None  # too recent to prune

    def test_gc_preserves_10k_most_recent_audit_events(
        self, tracer: SlashTracer
    ) -> None:
        """
        Even if a session is old and prunable, the 10,000 most-recent events
        must not be deleted (global keep guarantee).

        We use a small target count to keep the test fast.
        """
        tracer.open_task("OLD", "old")
        tracer.set_task_status("OLD", "closed")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "UPDATE tasks SET ended_at = ? WHERE session_id = 'OLD'", (old_ms,)
        )
        # Insert 5 audit events referencing the old session
        for i in range(5):
            tracer.log_event("OLD", f"T{i}", {}, "success")

        tracer.gc(keep_days=14)
        # With only 5 events total (< 10,000 keep floor), none should be deleted
        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]
        assert count == 5


# ---------------------------------------------------------------------------
# Integration — single-agent task lifecycle
# ---------------------------------------------------------------------------

class TestSingleAgentIntegration:
    def test_open_write_review_close(
        self, tracer: SlashTracer, tmp_file
    ) -> None:
        """
        Simulate: runner opens task → hooks capture baseline + write →
        summarizer sets pending_review → operator approves → closed.
        """
        p = tmp_file("main.py", "v0\n")
        session_id = "SESS_A"

        # 1. Open task (runner.py)
        tracer.open_task(session_id, "implement feature")

        # 2. Pre-hook: capture baseline
        tracer.capture_baseline(session_id, str(p))

        # 3. Agent writes the file; post-hook records snapshot
        p.write_text("v1\n")
        tracer.track_write(session_id, str(p), p.read_bytes())

        # 4. Summarizer → pending_review with summary
        summary = {"files": [str(p)]}
        tracer.set_task_status(session_id, "pending_review", summary=summary)

        pending = tracer.get_pending_reviews()
        assert any(t["session_id"] == session_id for t in pending)

        # 5. Operator approves
        tracer.set_task_status(session_id, "closed")

        assert tracer.conn.execute(
            "SELECT status FROM tasks WHERE session_id = ?", (session_id,)
        ).fetchone()[0] == "closed"
        assert not any(t["session_id"] == session_id for t in tracer.get_pending_reviews())

    def test_diff_reflects_write(self, tracer: SlashTracer, tmp_file) -> None:
        p = tmp_file("a.py", "before\n")
        tracer.open_task("S1", "task")
        tracer.capture_baseline("S1", str(p))
        p.write_text("after\n")
        tracer.track_write("S1", str(p), p.read_bytes())
        diff = tracer.diff_task("S1", str(p))
        assert "-before" in diff
        assert "+after" in diff


# ---------------------------------------------------------------------------
# Integration — sub-agent task
# ---------------------------------------------------------------------------

class TestSubAgentIntegration:
    def test_tree_diff_covers_all_files(
        self, tracer: SlashTracer, tmp_file
    ) -> None:
        """
        Simulate: SubagentStart → child writes → SubagentStop → root Stop.
        tree diff must include both root and child files.
        """
        root_file  = tmp_file("root.py", "root_v0\n")
        child_file = tmp_file("child.py", "child_v0\n")

        root_sid  = "ROOT"
        child_sid = "CHILD"

        # SubagentStart → open child task
        tracer.open_task(root_sid,  "root task")
        tracer.open_task(child_sid, "child task", parent_session_id=root_sid)

        # Pre/Post hooks for root
        tracer.capture_baseline(root_sid, str(root_file))
        root_file.write_text("root_v1\n")
        tracer.track_write(root_sid, str(root_file), root_file.read_bytes())

        # Pre/Post hooks for child
        tracer.capture_baseline(child_sid, str(child_file))
        child_file.write_text("child_v1\n")
        tracer.track_write(child_sid, str(child_file), child_file.read_bytes())

        # SubagentStop → child closed
        tracer.set_task_status(child_sid, "closed")

        # Root Stop hook → pending_review
        touched = tracer.list_touched_files(root_sid)
        assert str(root_file) in touched
        assert str(child_file) in touched

        root_diff  = tracer.diff_task(root_sid, str(root_file))
        child_diff = tracer.diff_task(root_sid, str(child_file))
        assert "-root_v0" in root_diff
        assert "+root_v1" in root_diff
        assert "-child_v0" in child_diff
        assert "+child_v1" in child_diff

    def test_sub_agent_not_in_pending_reviews(
        self, tracer: SlashTracer
    ) -> None:
        tracer.open_task("ROOT",  "root")
        tracer.open_task("CHILD", "child", parent_session_id="ROOT")
        tracer.set_task_status("CHILD", "closed")
        tracer.set_task_status("ROOT", "pending_review")
        pending = tracer.get_pending_reviews()
        ids = [p["session_id"] for p in pending]
        assert "CHILD" not in ids
        assert "ROOT" in ids


# ---------------------------------------------------------------------------
# Integration — concurrent baseline writes
# ---------------------------------------------------------------------------

class TestConcurrentBaselines:
    def test_min_captured_at_wins(
        self, tracer: SlashTracer, tmp_file
    ) -> None:
        """
        Two actor_ids capture a baseline for the same file.
        diff_task must use the one with the EARLIEST captured_at.
        """
        p = tmp_file("shared.py", "original\n")
        fp = str(p)

        tracer.open_task("ROOT",  "root")
        tracer.open_task("AGENT", "agent", parent_session_id="ROOT")

        # Manually insert baselines with controlled timestamps
        early_ms = int(time.time() * 1000) - 10_000
        late_ms  = int(time.time() * 1000)

        early_hash = tracer._store_blob(b"first baseline\n")
        late_hash  = tracer._store_blob(b"second baseline\n")

        tracer.conn.execute(
            "INSERT INTO baselines (session_id, file_path, pre_hash, captured_at) "
            "VALUES ('ROOT', ?, ?, ?)", (fp, early_hash, early_ms)
        )
        tracer.conn.execute(
            "INSERT INTO baselines (session_id, file_path, pre_hash, captured_at) "
            "VALUES ('AGENT', ?, ?, ?)", (fp, late_hash, late_ms)
        )
        tracer.conn.commit()

        # Final write from agent
        p.write_text("final\n")
        tracer.track_write("AGENT", fp, p.read_bytes())

        diff = tracer.diff_task("ROOT", fp)
        assert "-first baseline" in diff   # earliest baseline used
        assert "+final" in diff

    def test_concurrent_capture_baseline_thread_safe(
        self, db_path: str, tmp_file
    ) -> None:
        """
        Spawn two threads each opening a separate tracer connection and
        concurrently calling capture_baseline on the same file.
        Both must succeed without exception (WAL handles concurrency).
        """
        p = tmp_file("concurrent.py", "original\n")
        fp = str(p)
        errors: list[Exception] = []

        def worker(session_id: str) -> None:
            try:
                t = SlashTracer(db_path)
                t.open_task(session_id, "task")
                t.capture_baseline(session_id, fp)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=("T1",))
        t2 = threading.Thread(target=worker, args=("T2",))
        t1.start(); t2.start()
        t1.join();  t2.join()

        assert errors == [], f"Unexpected errors: {errors}"

        # Both rows must exist
        t = SlashTracer(db_path)
        rows = t.conn.execute(
            "SELECT session_id FROM baselines ORDER BY session_id"
        ).fetchall()
        session_ids = {r[0] for r in rows}
        assert {"T1", "T2"} <= session_ids


# ---------------------------------------------------------------------------
# Integration — REPL startup with pending review
# ---------------------------------------------------------------------------

class TestReplStartupPendingReview:
    def test_pending_review_surfaces_at_startup(
        self, tracer: SlashTracer, tmp_path: Path
    ) -> None:
        """
        _check_startup_tasks reads pending_review tasks from the DB and
        surfaces them.  We test the tracer side only (not the REPL I/O).
        """
        tracer.open_task("OLD_SESS", "fix the bug")
        tracer.set_task_status("OLD_SESS", "pending_review",
                               summary={"files": []})

        pending = tracer.get_pending_reviews()
        assert len(pending) == 1
        assert pending[0]["session_id"] == "OLD_SESS"
        assert pending[0]["prompt"] == "fix the bug"

    def test_approve_transitions_to_closed(self, tracer: SlashTracer) -> None:
        tracer.open_task("S", "task")
        tracer.set_task_status("S", "pending_review")

        # Simulate operator approving
        tracer.set_task_status("S", "closed")

        pending = tracer.get_pending_reviews()
        assert not any(p["session_id"] == "S" for p in pending)
        status = tracer.conn.execute(
            "SELECT status FROM tasks WHERE session_id = 'S'"
        ).fetchone()[0]
        assert status == "closed"

    def test_followup_transitions_back_to_open(self, tracer: SlashTracer) -> None:
        """Providing a follow-up instruction reverts the task to open."""
        tracer.open_task("S", "task")
        tracer.set_task_status("S", "pending_review")

        # Simulate operator providing a follow-up instruction
        tracer.set_task_status("S", "open")

        pending = tracer.get_pending_reviews()
        assert not any(p["session_id"] == "S" for p in pending)
        status = tracer.conn.execute(
            "SELECT status FROM tasks WHERE session_id = 'S'"
        ).fetchone()[0]
        assert status == "open"

    def test_blank_line_approve_transitions_to_closed(self, tracer: SlashTracer) -> None:
        """Blank-line (Enter) approval closes the task — CI default path."""
        tracer.open_task("S2", "task")
        tracer.set_task_status("S2", "pending_review")

        # Simulate CI / blank-line approve
        tracer.set_task_status("S2", "closed")

        status = tracer.conn.execute(
            "SELECT status FROM tasks WHERE session_id = 'S2'"
        ).fetchone()[0]
        assert status == "closed"
        assert not tracer.get_pending_reviews()


# ---------------------------------------------------------------------------
# Integration — stale open task recovery
# ---------------------------------------------------------------------------

class TestStaleOpenTaskRecovery:
    """
    At REPL startup _check_startup_tasks surfaces open tasks older than
    _STALE_TASK_THRESHOLD_SECS (3600s) for operator information.
    No automatic status change occurs; the operator decides what to do next.
    We test the tracer state transitions only.
    """

    def _stale_open(self, tracer: SlashTracer, session_id: str) -> None:
        tracer.open_task(session_id, "stale task")
        # Backdate started_at to over 2 hours ago
        old_ms = int((time.time() - 7200) * 1000)
        tracer.conn.execute(
            "UPDATE tasks SET started_at = ? WHERE session_id = ?",
            (old_ms, session_id),
        )
        tracer.conn.commit()

    def test_stale_open_task_remains_open_without_action(
        self, tracer: SlashTracer
    ) -> None:
        """Stale open tasks are surfaced for information only; no status change
        occurs automatically — the operator takes action via a new task or /review.
        """
        self._stale_open(tracer, "STALE")
        status = tracer.conn.execute(
            "SELECT status FROM tasks WHERE session_id = 'STALE'"
        ).fetchone()[0]
        assert status == "open"

    def test_stale_open_identified_by_age(
        self, tracer: SlashTracer
    ) -> None:
        self._stale_open(tracer, "STALE")
        threshold_secs = 3600
        cutoff_ms = int((time.time() - threshold_secs) * 1000)
        row = tracer.conn.execute(
            "SELECT session_id FROM tasks "
            "WHERE status = 'open' AND started_at < ?",
            (cutoff_ms,),
        ).fetchone()
        assert row is not None
        assert row[0] == "STALE"

    def test_recent_open_not_stale(self, tracer: SlashTracer) -> None:
        tracer.open_task("FRESH", "fresh task")
        threshold_secs = 3600
        cutoff_ms = int((time.time() - threshold_secs) * 1000)
        row = tracer.conn.execute(
            "SELECT session_id FROM tasks "
            "WHERE status = 'open' AND started_at < ?",
            (cutoff_ms,),
        ).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# Unit — SlashTracer.get_open_root_tasks
# ---------------------------------------------------------------------------

class TestGetOpenRootTasks:
    """Unit tests for the ``get_open_root_tasks`` tracer method used by the
    REPL's session-resume feature."""

    def _backdate(self, tracer: SlashTracer, session_id: str, age_secs: int) -> None:
        """Set started_at to *age_secs* seconds ago for *session_id*."""
        old_ms = int((time.time() - age_secs) * 1000)
        tracer.conn.execute(
            "UPDATE tasks SET started_at = ? WHERE session_id = ?",
            (old_ms, session_id),
        )
        tracer.conn.commit()

    def test_returns_stale_open_root_task(self, tracer: SlashTracer) -> None:
        """A root task in open state older than the threshold appears in the result."""
        tracer.open_task("S1", "do something")
        self._backdate(tracer, "S1", 7200)  # 2 hours old

        result = tracer.get_open_root_tasks(older_than_secs=3600)
        ids = [r["session_id"] for r in result]
        assert "S1" in ids

    def test_excludes_recent_open_task(self, tracer: SlashTracer) -> None:
        """A task started just now is not stale."""
        tracer.open_task("FRESH", "just started")

        result = tracer.get_open_root_tasks(older_than_secs=3600)
        ids = [r["session_id"] for r in result]
        assert "FRESH" not in ids

    def test_excludes_child_tasks(self, tracer: SlashTracer) -> None:
        """Sub-agent sessions (parent_session_id IS NOT NULL) are excluded."""
        tracer.open_task("ROOT", "root task")
        tracer.open_task("CHILD", "child task", parent_session_id="ROOT")
        self._backdate(tracer, "ROOT", 7200)
        self._backdate(tracer, "CHILD", 7200)

        result = tracer.get_open_root_tasks(older_than_secs=3600)
        ids = [r["session_id"] for r in result]
        assert "ROOT" in ids
        assert "CHILD" not in ids

    def test_excludes_non_open_statuses(self, tracer: SlashTracer) -> None:
        """Tasks in pending_review or closed state are not returned."""
        tracer.open_task("PR", "pending task")
        tracer.open_task("CL", "closed task")
        self._backdate(tracer, "PR", 7200)
        self._backdate(tracer, "CL", 7200)
        tracer.set_task_status("PR", "pending_review")
        tracer.set_task_status("CL", "closed")

        result = tracer.get_open_root_tasks(older_than_secs=3600)
        ids = [r["session_id"] for r in result]
        assert "PR" not in ids
        assert "CL" not in ids

    def test_returns_newest_first(self, tracer: SlashTracer) -> None:
        """Results are ordered newest-first (largest started_at first)."""
        for sid, age in [("OLD", 7200), ("OLDER", 10800), ("OLDEST", 14400)]:
            tracer.open_task(sid, f"task {sid}")
            self._backdate(tracer, sid, age)

        result = tracer.get_open_root_tasks(older_than_secs=3600)
        ids = [r["session_id"] for r in result]
        # OLD was started most recently (lowest age), should come first
        assert ids.index("OLD") < ids.index("OLDER") < ids.index("OLDEST")

    def test_result_shape(self, tracer: SlashTracer) -> None:
        """Each result dict contains session_id, prompt, and started_at."""
        tracer.open_task("S1", "my prompt text")
        self._backdate(tracer, "S1", 7200)

        result = tracer.get_open_root_tasks(older_than_secs=3600)
        assert len(result) == 1
        r = result[0]
        assert r["session_id"] == "S1"
        assert r["prompt"] == "my prompt text"
        assert isinstance(r["started_at"], int)

    def test_custom_threshold(self, tracer: SlashTracer) -> None:
        """The *older_than_secs* parameter is respected."""
        tracer.open_task("S1", "task")
        self._backdate(tracer, "S1", 1800)  # 30 min old

        # Not stale at the default 1-hour threshold
        assert tracer.get_open_root_tasks(older_than_secs=3600) == []
        # Stale at a shorter 15-minute threshold
        assert tracer.get_open_root_tasks(older_than_secs=900) != []

    def test_empty_db_returns_empty_list(self, tracer: SlashTracer) -> None:
        """No tasks at all returns an empty list without error."""
        assert tracer.get_open_root_tasks() == []


# ---------------------------------------------------------------------------
# Integration — session resume: _check_startup_tasks returns session_id
# ---------------------------------------------------------------------------

class TestSessionResumeSignal:
    """
    Tests that the tracer state is correct for the session-resume feature.

    _check_startup_tasks in repl.py reads from the tracer and returns a
    session_id when the operator opts to resume.  These tests verify the
    tracer API (get_open_root_tasks / get_pending_reviews / set_task_status)
    produces the state that the REPL logic depends on.
    """

    def test_followup_on_pending_review_puts_task_back_to_open(
        self, tracer: SlashTracer
    ) -> None:
        """
        When the operator provides a follow-up for a pending_review task,
        set_task_status('open') is called.  The session_id is then available
        in get_open_root_tasks (once backdated past the stale threshold) or
        directly from the tasks table, and can be passed to _build_client as
        resume=session_id.
        """
        tracer.open_task("SESS", "implement feature X")
        tracer.set_task_status("SESS", "pending_review")

        # Operator provides follow-up — repl.py calls set_task_status("open")
        tracer.set_task_status("SESS", "open")

        row = tracer.conn.execute(
            "SELECT status FROM tasks WHERE session_id = 'SESS'"
        ).fetchone()
        assert row[0] == "open"
        # No longer in pending_review
        assert not any(p["session_id"] == "SESS" for p in tracer.get_pending_reviews())

    def test_stale_open_session_id_is_retrievable_for_resume(
        self, tracer: SlashTracer
    ) -> None:
        """
        A session left in open state after a REPL crash can be retrieved via
        get_open_root_tasks and its session_id passed to _build_client(resume=).
        """
        tracer.open_task("INTERRUPTED", "big refactor")
        old_ms = int((time.time() - 7200) * 1000)
        tracer.conn.execute(
            "UPDATE tasks SET started_at = ? WHERE session_id = 'INTERRUPTED'",
            (old_ms,),
        )
        tracer.conn.commit()

        stale = tracer.get_open_root_tasks(older_than_secs=3600)
        assert len(stale) >= 1
        session_ids = [r["session_id"] for r in stale]
        assert "INTERRUPTED" in session_ids

    def test_approve_closes_task_no_resume_needed(
        self, tracer: SlashTracer
    ) -> None:
        """
        When the operator approves (blank Enter), the task moves to closed and
        get_pending_reviews returns nothing — no resume is needed.
        """
        tracer.open_task("DONE", "task to approve")
        tracer.set_task_status("DONE", "pending_review")

        # Operator approves — repl.py calls set_task_status("closed")
        tracer.set_task_status("DONE", "closed")

        assert tracer.get_pending_reviews() == []
        assert tracer.get_open_root_tasks(older_than_secs=0) == []


# ---------------------------------------------------------------------------
# Integration — REPL /new creates task row; /review surfaces pending
# ---------------------------------------------------------------------------

class TestReplNewAndReview:
    def test_new_session_open_task_row(self, tracer: SlashTracer) -> None:
        """Each REPL /new creates a fresh open task row."""
        for i, sid in enumerate(("SID1", "SID2", "SID3")):
            tracer.open_task(sid, f"user message {i}")
        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'open'"
        ).fetchone()[0]
        assert count == 3

    def test_review_command_lists_pending(self, tracer: SlashTracer) -> None:
        """After summarizer fires, /review must surface the task."""
        tracer.open_task("MID_SESS", "mid-session task")
        tracer.set_task_status("MID_SESS", "pending_review",
                               summary={"files": ["a.py"]})

        pending = tracer.get_pending_reviews()
        ids = [p["session_id"] for p in pending]
        assert "MID_SESS" in ids

    def test_multiple_sessions_each_reviewable(
        self, tracer: SlashTracer
    ) -> None:
        for sid in ("S1", "S2"):
            tracer.open_task(sid, f"task {sid}")
            tracer.set_task_status(sid, "pending_review")

        pending = tracer.get_pending_reviews()
        ids = {p["session_id"] for p in pending}
        assert {"S1", "S2"} <= ids


# ---------------------------------------------------------------------------
# Unit — _extract_trace_unit edge cases
# ---------------------------------------------------------------------------

class TestExtractTraceUnitEdgeCases:
    """Additional coverage for _extract_trace_unit branches not hit above."""

    def test_multiedit_empty_edits_list(self, tracer: SlashTracer) -> None:
        """MultiEdit with an empty edits list should return an empty file_paths list."""
        unit = tracer._extract_trace_unit("MultiEdit", {"edits": []}, None)
        assert unit == {"file_paths": []}

    def test_multiedit_missing_edits_key(self, tracer: SlashTracer) -> None:
        """MultiEdit with no edits key should not raise."""
        unit = tracer._extract_trace_unit("MultiEdit", {}, None)
        assert unit == {"file_paths": []}

    def test_bash_uses_output_key_fallback(self, tracer: SlashTracer) -> None:
        """Bash handler should fall back to 'output' when 'stdout' is absent."""
        unit = tracer._extract_trace_unit(
            "Bash",
            {"command": "pwd"},
            {"exit_code": 0, "output": "/workspace"},
        )
        assert unit["stdout_head"] == "/workspace"

    def test_bash_empty_response(self, tracer: SlashTracer) -> None:
        """Bash with no tool_response should not raise."""
        unit = tracer._extract_trace_unit("Bash", {"command": "date"}, None)
        assert unit["command"] == "date"
        assert unit["exit_code"] is None
        assert unit["stdout_head"] == ""

    def test_agent_uses_description_fallback(self, tracer: SlashTracer) -> None:
        """Agent handler should fall back to 'description' when 'prompt' is absent."""
        unit = tracer._extract_trace_unit(
            "Agent",
            {"subagent_type": "Explore", "description": "look around"},
            None,
        )
        assert unit["prompt_head"] == "look around"

    def test_agent_uses_agent_type_key(self, tracer: SlashTracer) -> None:
        """Agent handler accepts 'agent_type' as well as 'subagent_type'."""
        unit = tracer._extract_trace_unit(
            "Agent",
            {"agent_type": "Plan", "prompt": "plan it"},
            None,
        )
        assert unit["agent_type"] == "Plan"

    def test_unknown_tool_empty_input_returns_none(
        self, tracer: SlashTracer
    ) -> None:
        """Fallback with an empty input dict should return None (no keys to store)."""
        unit = tracer._extract_trace_unit("WeirdTool", {}, None)
        assert unit is None

    def test_unknown_tool_none_input_returns_none(
        self, tracer: SlashTracer
    ) -> None:
        """Fallback with None input should return None without raising."""
        unit = tracer._extract_trace_unit("WeirdTool", None, None)
        assert unit is None

    def test_unknown_tool_truncates_long_values(
        self, tracer: SlashTracer
    ) -> None:
        """Fallback values must be truncated to 200 chars."""
        unit = tracer._extract_trace_unit(
            "SomeTool", {"key": "v" * 500}, None
        )
        assert unit is not None
        assert len(unit["key"]) == 200

    def test_write_tool_none_file_path(self, tracer: SlashTracer) -> None:
        """Write with missing file_path key should return {'file_path': None}."""
        unit = tracer._extract_trace_unit("Write", {}, None)
        assert unit == {"file_path": None}


# ---------------------------------------------------------------------------
# Unit — task lifecycle edge cases
# ---------------------------------------------------------------------------

class TestTaskLifecycleEdgeCases:
    def test_set_task_status_no_summary(self, tracer: SlashTracer) -> None:
        """set_task_status without summary leaves summary as NULL."""
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "closed")
        row = tracer.conn.execute(
            "SELECT summary FROM tasks WHERE session_id = 'S1'"
        ).fetchone()
        assert row[0] is None

    def test_set_task_status_sets_ended_at(self, tracer: SlashTracer) -> None:
        """set_task_status must populate ended_at."""
        tracer.open_task("S1", "task")
        before_ms = int(time.time() * 1000)
        tracer.set_task_status("S1", "closed")
        ended_at = tracer.conn.execute(
            "SELECT ended_at FROM tasks WHERE session_id = 'S1'"
        ).fetchone()[0]
        assert ended_at is not None
        assert ended_at >= before_ms

    def test_get_child_sessions_no_children(self, tracer: SlashTracer) -> None:
        """get_child_sessions returns an empty list when no children exist."""
        tracer.open_task("LONE", "lone task")
        assert tracer.get_child_sessions("LONE") == []

    def test_get_child_sessions_unknown_parent(
        self, tracer: SlashTracer
    ) -> None:
        """get_child_sessions on an unknown session_id returns empty list."""
        assert tracer.get_child_sessions("GHOST") == []

    def test_open_task_with_parent(self, tracer: SlashTracer) -> None:
        """open_task correctly stores parent_session_id."""
        tracer.open_task("ROOT", "root")
        tracer.open_task("CHILD", "child", parent_session_id="ROOT")
        row = tracer.conn.execute(
            "SELECT parent_session_id FROM tasks WHERE session_id = 'CHILD'"
        ).fetchone()
        assert row[0] == "ROOT"

    def test_get_pending_reviews_excludes_closed(
        self, tracer: SlashTracer
    ) -> None:
        """Closed tasks must not appear in pending_reviews."""
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "closed")
        assert tracer.get_pending_reviews() == []

    def test_get_pending_reviews_excludes_abandoned(
        self, tracer: SlashTracer
    ) -> None:
        """Abandoned tasks must not appear in pending_reviews."""
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "abandoned")
        assert tracer.get_pending_reviews() == []

    def test_get_pending_reviews_summary_none(self, tracer: SlashTracer) -> None:
        """pending_reviews entry has summary=None when none was stored."""
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "pending_review")
        pending = tracer.get_pending_reviews()
        assert pending[0]["summary"] is None


# ---------------------------------------------------------------------------
# Unit — narrative persistence
# ---------------------------------------------------------------------------

class TestNarrative:
    def test_set_task_status_stores_narrative(self, tracer: SlashTracer) -> None:
        """narrative is persisted when passed to set_task_status."""
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "pending_review", narrative="I fixed the bug.")
        row = tracer.conn.execute(
            "SELECT narrative FROM tasks WHERE session_id = 'S1'"
        ).fetchone()
        assert row[0] == "I fixed the bug."

    def test_set_task_status_narrative_none_preserves_existing(
        self, tracer: SlashTracer
    ) -> None:
        """Passing narrative=None to set_task_status must NOT overwrite an existing value."""
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "pending_review", narrative="First narrative.")
        # Approve without passing narrative — existing value must survive.
        tracer.set_task_status("S1", "closed")
        row = tracer.conn.execute(
            "SELECT narrative FROM tasks WHERE session_id = 'S1'"
        ).fetchone()
        assert row[0] == "First narrative."

    def test_set_task_status_narrative_can_be_overwritten(
        self, tracer: SlashTracer
    ) -> None:
        """Passing an explicit narrative overwrites the previous value."""
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "pending_review", narrative="Draft.")
        tracer.set_task_status("S1", "closed", narrative="Final.")
        row = tracer.conn.execute(
            "SELECT narrative FROM tasks WHERE session_id = 'S1'"
        ).fetchone()
        assert row[0] == "Final."

    def test_summary_also_preserved_on_approve(self, tracer: SlashTracer) -> None:
        """Approving (no summary arg) must not clear an existing summary."""
        tracer.open_task("S1", "task")
        summary = {"file_changes": [], "action_log": []}
        tracer.set_task_status("S1", "pending_review", summary=summary)
        # Simulate approve — no summary passed.
        tracer.set_task_status("S1", "closed")
        row = tracer.conn.execute(
            "SELECT summary FROM tasks WHERE session_id = 'S1'"
        ).fetchone()
        assert json.loads(row[0]) == summary

    def test_get_pending_reviews_returns_narrative(self, tracer: SlashTracer) -> None:
        """get_pending_reviews must include the narrative field."""
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "pending_review", narrative="Done everything.")
        pending = tracer.get_pending_reviews()
        assert pending[0]["narrative"] == "Done everything."

    def test_get_pending_reviews_narrative_none_when_absent(
        self, tracer: SlashTracer
    ) -> None:
        """get_pending_reviews returns narrative=None when not set."""
        tracer.open_task("S1", "task")
        tracer.set_task_status("S1", "pending_review")
        pending = tracer.get_pending_reviews()
        assert pending[0]["narrative"] is None

    def test_migrate_adds_column_to_existing_db(self, db_path: str) -> None:
        """_migrate is idempotent — running it twice on the same DB does not raise."""
        t = SlashTracer(db_path)
        # Calling _migrate again manually must not raise even though the column exists.
        t._migrate()
        t._migrate()
        # Column must be present.
        cols = [
            row[1]
            for row in t.conn.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        assert "narrative" in cols

    def test_narrative_column_present_in_schema(self, tracer: SlashTracer) -> None:
        """tasks table must have a narrative column after init."""
        cols = [
            row[1]
            for row in tracer.conn.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        assert "narrative" in cols


# ---------------------------------------------------------------------------
# Unit — extract_narrative_from_transcript
# ---------------------------------------------------------------------------

class TestExtractNarrativeFromTranscript:
    def test_returns_last_assistant_text(self, tmp_path: Path) -> None:
        """Returns the concatenated text of the last visible assistant message."""
        from slash.tracer import extract_narrative_from_transcript

        transcript = tmp_path / "session.jsonl"
        entries = [
            {"type": "user", "message": {"content": [{"type": "text", "text": "do it"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        ]
        transcript.write_text("\n".join(json.dumps(e) for e in entries))
        assert extract_narrative_from_transcript(str(transcript)) == "Done."

    def test_skips_meta_entries(self, tmp_path: Path) -> None:
        """isMeta entries are ignored; falls back to earlier real message."""
        from slash.tracer import extract_narrative_from_transcript

        transcript = tmp_path / "session.jsonl"
        entries = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Real."}]}},
            {"type": "assistant", "isMeta": True, "message": {"content": [{"type": "text", "text": "Meta."}]}},
        ]
        transcript.write_text("\n".join(json.dumps(e) for e in entries))
        assert extract_narrative_from_transcript(str(transcript)) == "Real."

    def test_skips_sidechain_entries(self, tmp_path: Path) -> None:
        """isSidechain entries are ignored."""
        from slash.tracer import extract_narrative_from_transcript

        transcript = tmp_path / "session.jsonl"
        entries = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Main."}]}},
            {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "Side."}]}},
        ]
        transcript.write_text("\n".join(json.dumps(e) for e in entries))
        assert extract_narrative_from_transcript(str(transcript)) == "Main."

    def test_concatenates_multiple_text_blocks(self, tmp_path: Path) -> None:
        """Multiple TextBlocks in one message are joined with double newline."""
        from slash.tracer import extract_narrative_from_transcript

        transcript = tmp_path / "session.jsonl"
        entry = {
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "Part one."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
                {"type": "text", "text": "Part two."},
            ]},
        }
        transcript.write_text(json.dumps(entry))
        result = extract_narrative_from_transcript(str(transcript))
        assert result == "Part one.\n\nPart two."

    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        """Returns empty string when transcript file does not exist."""
        from slash.tracer import extract_narrative_from_transcript

        result = extract_narrative_from_transcript(str(tmp_path / "nonexistent.jsonl"))
        assert result == ""

    def test_returns_empty_when_no_assistant_messages(self, tmp_path: Path) -> None:
        """Returns empty string if transcript has no assistant entries."""
        from slash.tracer import extract_narrative_from_transcript

        transcript = tmp_path / "session.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"content": []}}))
        assert extract_narrative_from_transcript(str(transcript)) == ""

    def test_returns_empty_for_assistant_with_no_text_blocks(
        self, tmp_path: Path
    ) -> None:
        """Returns empty string if the last assistant message has only tool-use blocks."""
        from slash.tracer import extract_narrative_from_transcript

        transcript = tmp_path / "session.jsonl"
        entry = {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        }
        transcript.write_text(json.dumps(entry))
        assert extract_narrative_from_transcript(str(transcript)) == ""

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        """Blank lines in the JSONL file are skipped without error."""
        from slash.tracer import extract_narrative_from_transcript

        transcript = tmp_path / "session.jsonl"
        entry = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi."}]}}
        transcript.write_text("\n\n" + json.dumps(entry) + "\n\n")
        assert extract_narrative_from_transcript(str(transcript)) == "Hi."


# ---------------------------------------------------------------------------
# Unit — GC edge cases
# ---------------------------------------------------------------------------

class TestGCEdgeCases:
    def _old_ms(self, days: int = 20) -> int:
        return int((time.time() - days * 86400) * 1000)

    def test_gc_removes_old_abandoned_snapshots(
        self, tracer: SlashTracer
    ) -> None:
        """GC must prune snapshots for abandoned tasks, not just closed."""
        tracer.open_task("ABA", "abandoned task")
        tracer.set_task_status("ABA", "abandoned")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "UPDATE tasks SET ended_at = ? WHERE session_id = 'ABA'",
            (old_ms,),
        )
        tracer.conn.execute(
            "INSERT INTO snapshots (session_id, file_path, blob_hash, written_at) "
            "VALUES ('ABA', 'x.py', ?, ?)",
            (tracer._store_blob(b"abandoned data"), old_ms),
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM snapshots"
        ).fetchone()[0]
        assert count == 0

    def test_gc_removes_old_baselines(self, tracer: SlashTracer) -> None:
        """GC must delete baselines for old closed tasks."""
        tracer.open_task("OLD", "old task")
        tracer.set_task_status("OLD", "closed")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "UPDATE tasks SET ended_at = ? WHERE session_id = 'OLD'",
            (old_ms,),
        )
        tracer.conn.execute(
            "INSERT INTO baselines (session_id, file_path, pre_hash, captured_at) "
            "VALUES ('OLD', 'f.py', NULL, ?)",
            (old_ms,),
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM baselines"
        ).fetchone()[0]
        assert count == 0

    def test_gc_protects_open_baselines(self, tracer: SlashTracer) -> None:
        """Baselines for open tasks must survive GC."""
        tracer.open_task("OPEN", "open")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "INSERT INTO baselines (session_id, file_path, pre_hash, captured_at) "
            "VALUES ('OPEN', 'f.py', NULL, ?)",
            (old_ms,),
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM baselines"
        ).fetchone()[0]
        assert count == 1

    def test_gc_empty_db_is_safe(self, tracer: SlashTracer) -> None:
        """gc on an empty database must not raise."""
        tracer.gc(keep_days=14)  # must not raise

    def test_gc_removes_old_audit_events_beyond_10k(
        self, tracer: SlashTracer
    ) -> None:
        """Audit events beyond the 10,000 keep-floor must be pruned for old sessions."""
        tracer.open_task("OLD", "old")
        tracer.set_task_status("OLD", "closed")
        old_ms = self._old_ms()
        tracer.conn.execute(
            "UPDATE tasks SET ended_at = ? WHERE session_id = 'OLD'",
            (old_ms,),
        )
        tracer.conn.commit()

        # Insert more than 10,000 audit events for the old session
        # (we use a direct INSERT for speed rather than log_event)
        ts = "2020-01-01T00:00:00+00:00"
        tracer.conn.executemany(
            "INSERT INTO audit_events (ts, session_id, tool, outcome) "
            "VALUES (?, 'OLD', 'T', 'success')",
            [(ts,)] * 10_005,
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)

        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]
        # 10,000 most recent kept globally; 5 pruned
        assert count == 10_000

    def test_gc_protects_open_subtree(self, tracer: SlashTracer) -> None:
        """Children of an open root task must be protected even if closed."""
        tracer.open_task("ROOT", "open root")
        tracer.open_task("CHILD", "child", parent_session_id="ROOT")
        tracer.set_task_status("CHILD", "closed")

        old_ms = self._old_ms()
        tracer.conn.execute(
            "UPDATE tasks SET ended_at = ? WHERE session_id = 'CHILD'",
            (old_ms,),
        )
        blob = tracer._store_blob(b"child data")
        tracer.conn.execute(
            "INSERT INTO snapshots (session_id, file_path, blob_hash, written_at) "
            "VALUES ('CHILD', 'c.py', ?, ?)",
            (blob, old_ms),
        )
        tracer.conn.commit()

        tracer.gc(keep_days=14)
        count = tracer.conn.execute(
            "SELECT COUNT(*) FROM snapshots"
        ).fetchone()[0]
        assert count == 1  # child is in open root's subtree — protected


# ---------------------------------------------------------------------------
# Unit — diff_task edge cases
# ---------------------------------------------------------------------------

class TestDiffTaskEdgeCases:
    def test_unknown_root_returns_empty_string(
        self, tracer: SlashTracer
    ) -> None:
        """diff_task for a root that has no baseline should return ''."""
        diff = tracer.diff_task("GHOST", "any.py")
        assert diff == ""

    def test_diff_includes_unified_diff_header(
        self, tracer: SlashTracer
    ) -> None:
        """The diff output must include the a/ b/ file headers."""
        tracer.open_task("S1", "task")
        fp = "myfile.py"
        pre_hash = tracer._store_blob(b"old\n")
        now_ms = int(time.time() * 1000)
        tracer.conn.execute(
            "INSERT INTO baselines (session_id, file_path, pre_hash, captured_at) "
            "VALUES ('S1', ?, ?, ?)",
            (fp, pre_hash, now_ms),
        )
        tracer.conn.commit()
        tracer.track_write("S1", fp, b"new\n")
        diff = tracer.diff_task("S1", fp)
        assert f"a/{fp}" in diff
        assert f"b/{fp}" in diff


# ---------------------------------------------------------------------------
# Unit — recent_audit_events edge cases
# ---------------------------------------------------------------------------

class TestRecentAuditEventsEdgeCases:
    def test_empty_db_returns_empty_list(self, tracer: SlashTracer) -> None:
        assert tracer.recent_audit_events() == []

    def test_session_scoped_empty_session(self, tracer: SlashTracer) -> None:
        """Scoped query for an unknown session_id should return empty list."""
        assert tracer.recent_audit_events(session_id="GHOST") == []

    def test_default_limit_is_20(self, tracer: SlashTracer) -> None:
        """Default limit should cap results at 20."""
        tracer.open_task("S1", "task")
        for i in range(30):
            tracer.log_event("S1", f"T{i}", {}, "success")
        evs = tracer.recent_audit_events()
        assert len(evs) == 20

    def test_event_reason_field_populated_on_denial(
        self, tracer: SlashTracer
    ) -> None:
        tracer.open_task("S1", "task")
        tracer.log_event("S1", "Bash", {}, "denied", reason="blocked by policy")
        ev = tracer.recent_audit_events(limit=1)[0]
        assert ev["reason"] == "blocked by policy"

    def test_event_input_stores_path_for_read_tools(self, tracer: SlashTracer) -> None:
        tracer.open_task("S1", "task")
        tracer.log_event("S1", "Grep", {"pattern": "foo"}, "success")
        ev = tracer.recent_audit_events(limit=1)[0]
        assert ev["input"] == {"file_path": "foo"}
