"""
contAIned payload — helpers for assembling and inspecting ATP work unit payloads.

Consumed by the ``contAIned payload`` REPL command and by the stop hook's
push-processing path.  Functions can also be called directly from other modules.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _find_tracer_db(root: Path) -> Path | None:
    """Walk up from *root* looking for a .contAIned/tracer.db."""
    current = root.resolve()
    while current != current.parent:
        candidate = current / ".contAIned" / "tracer.db"
        if candidate.exists():
            return candidate
        current = current.parent
    return None


def payload_show_impl(
    db: Path | None,
    work_unit_id: str,
    output_path: str | None = None,
) -> None:
    """Assemble the ATP payload for *work_unit_id* and print or write it."""
    if db is None:
        print(
            "Error: tracer.db not found. Run from inside a contAIned workspace.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from contained.tracer import contAInedTracer  # noqa: PLC0415

        tracer = contAInedTracer(str(db))
        payload = tracer.assemble_payload(work_unit_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error assembling payload: {exc}", file=sys.stderr)
        sys.exit(1)

    payload_json = json.dumps(payload, indent=2, ensure_ascii=False)
    if output_path:
        Path(output_path).write_text(payload_json, encoding="utf-8")
        print(f"Payload written to {output_path}")
    else:
        print(payload_json)


def payload_list_impl(db: Path | None, show_all: bool = False) -> None:
    """List work units in the tracer database."""
    if db is None:
        print(
            "Error: tracer.db not found. Run from inside a contAIned workspace.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from contained.tracer import contAInedTracer  # noqa: PLC0415

        tracer = contAInedTracer(str(db))
        status_filter = "" if show_all else "WHERE status = 'open'"
        rows = tracer.conn.execute(
            f"""
            SELECT id, status, branch, base_commit, head_commit, opened_at, prompt
            FROM work_units
            {status_filter}
            ORDER BY opened_at DESC
            """
        ).fetchall()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print("No work units found.")
        return

    for row in rows:
        wu_id, status, branch, base, head, opened_at, prompt = row
        head_short = (head or "")[:8] or "(open)"
        base_short = (base or "")[:8]
        print(f"[{status}] {wu_id[:8]}… {branch}  {base_short}→{head_short}  {(prompt or '')[:60]}")
