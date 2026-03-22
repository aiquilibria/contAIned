"""
contAIned verify — confirm workspace image integrity before a session.

Checks that the local ``contained:latest`` image matches the signed digest
recorded in ``.contAIned/provenance.yaml``, then re-verifies the Sigstore
signature via ``cosign verify-blob``.

This is a host-side command: it needs Docker (to inspect the local image)
and cosign (to query Rekor).  It does not run inside the container.

Exit codes:
  0  — provenance verified, or Sigstore was disabled at init (not an error)
  1  — verification failed or provenance is missing/inconsistent
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from contained.docker_runner import _find_cosign, _find_docker
from contained.sigstore import _get_image_id, cosign_verify_blob

console = Console()


def _verify_workspace(target: Path) -> dict | None:
    """
    Run all provenance checks without printing anything.

    Returns the provenance dict on success, or ``None`` if Sigstore was
    disabled at init (not an error — caller should skip silently).

    Raises ``RuntimeError`` with a human-readable message on any failure.
    """
    import yaml

    target = target.resolve()

    manifest_path = target / ".contAIned" / "manifest.yaml"
    if not manifest_path.exists():
        raise RuntimeError("No manifest found. Is this a contAIned workspace?")

    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    sigstore_cfg = manifest.get("policy", {}).get("sigstore") or manifest.get("sigstore", {})

    if not sigstore_cfg.get("enabled", False):
        return None  # disabled — not an error

    provenance_path = target / ".contAIned" / "provenance.yaml"
    if not provenance_path.exists():
        raise RuntimeError(
            "provenance.yaml not found despite Sigstore being enabled. "
            "Re-run contAIned init to generate provenance."
        )

    provenance = yaml.safe_load(provenance_path.read_text()) or {}
    expected_digest = provenance.get("image_digest", "")

    try:
        docker_bin = _find_docker()
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc

    image = manifest.get("runtime", {}).get("docker", {}).get("image", "contained:latest")
    try:
        actual_digest = _get_image_id(docker_bin, image)
    except RuntimeError as exc:
        raise RuntimeError(f"Could not inspect image: {exc}") from exc

    if actual_digest != expected_digest:
        raise RuntimeError(
            f"Image digest mismatch — image has been replaced since init.\n"
            f"  expected: {expected_digest}\n"
            f"  actual:   {actual_digest}"
        )

    bundle_path = target / ".contAIned" / "provenance.bundle"
    if not bundle_path.exists():
        raise RuntimeError("provenance.bundle not found. Re-run contAIned init to regenerate.")

    try:
        _find_cosign()
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc

    identity = provenance.get("operator_identity", "")
    oidc_issuer = provenance.get("oidc_issuer", "")
    rekor_url = sigstore_cfg.get("rekor_url", "https://rekor.sigstore.dev")

    try:
        cosign_verify_blob(bundle_path, expected_digest, identity, oidc_issuer, rekor_url)
    except RuntimeError as exc:
        raise RuntimeError(f"Sigstore verification failed: {exc}") from exc

    return provenance


def run_verify(target: Path) -> None:
    """
    Verify workspace image provenance (verbose, standalone command).

    Reads the manifest and provenance record from ``target/.contAIned/``,
    compares the current image digest, and validates the Sigstore signature.
    """
    target = target.resolve()
    console.print(f"\n[bold]contAIned verify[/bold] — [dim]{target}[/dim]\n")

    try:
        provenance = _verify_workspace(target)
    except RuntimeError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise SystemExit(1)

    if provenance is None:
        console.print(
            "[dim]Build provenance was disabled at init — nothing to verify.[/dim]\n"
            "[dim]Re-run contAIned init and enable Sigstore to record provenance.[/dim]"
        )
        return

    digest = provenance.get("image_digest", "")
    console.print(f"  [green]✓[/green] Image digest matches  [dim]{digest[:26]}…[/dim]")
    console.print("  [green]✓[/green] Sigstore signature verified")
    console.print(f"\n  operator : {provenance.get('operator_identity', '')}")
    console.print(f"  issuer   : {provenance.get('oidc_issuer', '')}")
    console.print(f"  signed   : {provenance.get('signed_at', 'unknown')}")
    console.print(
        f"  Rekor    : entry {provenance.get('rekor_log_index', '?')} "
        f"[dim]({provenance.get('rekor_entry_url', '')})[/dim]"
    )
    console.print()
