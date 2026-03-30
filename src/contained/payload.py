"""
contAIned payload — helpers for assembling, inspecting, and submitting ATP work unit payloads.

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


def _resolve_mainlined_url(manifest_path: Path) -> str | None:
    """Resolve the mAInlined submission base URL from *manifest_path*.

    Prefers the in-container Docker-network URL from ``mainlined.policy_yaml``
    (e.g. ``http://mainlined:8080``) and grafts the path from ``mainlined.url``
    (the host-side bootstrap URL) so the org/workspace segment is preserved.
    Returns ``None`` when the manifest is absent or contains no usable URL.
    """
    if not manifest_path.exists():
        return None
    try:
        from urllib.parse import urlparse, urlunparse  # noqa: PLC0415

        import yaml as _yaml  # noqa: PLC0415

        manifest = _yaml.safe_load(manifest_path.read_text()) or {}
        mainlined_sec = manifest.get("mainlined", {})
        bootstrap_url = str(mainlined_sec.get("url", "") or "")
        policy_base_url = ""
        policy_yaml_str = mainlined_sec.get("policy_yaml", "")
        if policy_yaml_str:
            try:
                policy_doc = _yaml.safe_load(policy_yaml_str) or {}
                policy_base_url = str(
                    policy_doc.get("policy", {}).get("mAInlined", {}).get("url", "") or ""
                )
            except Exception:
                pass
        if policy_base_url and bootstrap_url:
            pb = urlparse(policy_base_url)
            pf = urlparse(bootstrap_url)
            return urlunparse((pb.scheme, pb.netloc, pf.path, pf.params, pf.query, pf.fragment))
        url = policy_base_url or bootstrap_url
        return url if url else None
    except Exception:
        return None


def submit_proof_impl(
    db: Path | None,
    work_unit_id: str,
    cwd: Path | None = None,
    *,
    secrets_dir: Path = Path("/run/contained/secrets"),
) -> None:
    """Assemble and POST the ATP proof for *work_unit_id* to the mAInlined endpoint.

    URL resolution order (same as the stop hook):
      1. ``mainlined.policy_yaml`` → ``policy.mAInlined.url`` (Docker-network alias)
         with the path grafted from ``mainlined.url`` (host-side bootstrap URL).
      2. Whichever single source is present if only one is set.

    The API key is read from ``secrets_dir/mainlined_api_key``.
    Exits with a non-zero status on any unrecoverable error.
    """
    if db is None:
        print(
            "Error: tracer.db not found. Run from inside a contAIned workspace.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve workspace root for manifest lookup.
    workspace = cwd or db.parent.parent

    try:
        from contained.tracer import contAInedTracer  # noqa: PLC0415

        tracer = contAInedTracer(str(db))
        payload = tracer.assemble_proof(work_unit_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error assembling proof: {exc}", file=sys.stderr)
        sys.exit(1)

    manifest_path = workspace / ".contAIned" / "manifest.yaml"
    base_url = _resolve_mainlined_url(manifest_path)
    if not base_url:
        print("Error: could not resolve mAInlined URL from manifest.yaml", file=sys.stderr)
        sys.exit(1)
    submit_url = base_url.rstrip("/") + "/proof/submit"

    key_path = secrets_dir / "mainlined_api_key"
    if not key_path.exists():
        print(f"Error: API key not found at {key_path}", file=sys.stderr)
        sys.exit(1)
    api_key = key_path.read_text().strip()

    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(
        submit_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        print(f"Submitted to {submit_url} — HTTP {resp.status}")
        if body:
            print(body)
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        body = exc.read().decode("utf-8", errors="replace")
        print(f"Error: HTTP {exc.code} from {submit_url}: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error posting to {submit_url}: {exc}", file=sys.stderr)
        sys.exit(1)


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
