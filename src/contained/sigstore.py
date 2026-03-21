"""
contAIned Sigstore integration — keyless image signing and provenance extraction.

Invoked by ``contained init`` when ``sigstore.enabled`` is true in the manifest.
All operations run on the host (not inside the container).

The image is local-only and never pushed to a registry.  We sign the image
digest string as a blob using ``cosign sign-blob``, which creates a Rekor
transparency log entry binding: image digest ↔ operator OIDC identity ↔ timestamp.

The resulting bundle is parsed to extract the Rekor log index and the Fulcio
certificate, from which the operator identity and OIDC issuer are extracted.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _get_image_id(docker_bin: str, image: str) -> str:
    """Return the sha256 image ID for a local Docker image."""
    result = subprocess.run(
        [docker_bin, "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker image inspect failed: {result.stderr.strip()}")
    image_id = result.stdout.strip()
    if not image_id.startswith("sha256:"):
        raise RuntimeError(f"Unexpected image ID format: {image_id!r}")
    return image_id


def _parse_fulcio_cert(cert_pem: str) -> tuple[str, str]:
    """
    Extract ``(identity, oidc_issuer)`` from a Fulcio PEM certificate.

    Identity is taken from the Subject Alternative Name extension (email or URI).
    OIDC issuer is stored in the Sigstore OID ``1.3.6.1.4.1.57264.1.1``.

    Uses ``openssl`` as a subprocess — no extra Python dependencies required.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(cert_pem)
        cert_path = Path(f.name)

    try:
        san_result = subprocess.run(
            ["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(cert_path)],
            capture_output=True,
            text=True,
        )
        text_result = subprocess.run(
            ["openssl", "x509", "-noout", "-text", "-in", str(cert_path)],
            capture_output=True,
            text=True,
        )
    finally:
        cert_path.unlink(missing_ok=True)

    identity = _extract_san(san_result.stdout)
    issuer = _extract_oidc_issuer(text_result.stdout)
    return identity, issuer


def _extract_san(openssl_output: str) -> str:
    """Parse operator identity from openssl subjectAltName output."""
    for line in openssl_output.splitlines():
        # openssl prints SANs as comma-separated values on one line
        for part in line.split(","):
            part = part.strip()
            if part.startswith("email:"):
                return part[len("email:") :]
            if part.startswith("URI:"):
                return part[len("URI:") :]
    return "unknown"


def _extract_oidc_issuer(openssl_text: str) -> str:
    """Parse OIDC issuer from openssl -text output (OID 1.3.6.1.4.1.57264.1.1)."""
    lines = openssl_text.splitlines()
    for i, line in enumerate(lines):
        if "1.3.6.1.4.1.57264.1.1" in line:
            # Value is on the next non-empty line
            for j in range(i + 1, min(i + 3, len(lines))):
                value = lines[j].strip()
                if value and not value.startswith(".."):
                    return value
    return "unknown"


def cosign_verify_blob(
    bundle_path: Path,
    image_digest: str,
    identity: str,
    oidc_issuer: str,
    rekor_url: str,
) -> None:
    """
    Verify a blob signature produced by ``cosign_sign``.

    Reconstructs the original payload (the image digest string), then runs
    ``cosign verify-blob --bundle <bundle_path>`` against it.

    Raises ``RuntimeError`` with cosign's output on failure.
    """
    from contained.docker_runner import _find_cosign

    cosign_bin = _find_cosign()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(image_digest)
        payload_path = Path(f.name)

    try:
        result = subprocess.run(
            [
                cosign_bin,
                "verify-blob",
                "--bundle",
                str(bundle_path),
                "--certificate-identity",
                identity,
                "--certificate-oidc-issuer",
                oidc_issuer,
                "--rekor-url",
                rekor_url,
                str(payload_path),
            ],
            capture_output=True,
            text=True,
        )
    finally:
        payload_path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def cosign_sign(
    image: str, rekor_url: str, fulcio_url: str, bundle_dest: Path | None = None
) -> dict:
    """
    Sign the image digest as a blob using cosign keyless signing.

    Runs the OIDC browser flow (or device flow in non-TTY contexts).
    Creates a Rekor transparency log entry and writes a local bundle file.
    Parses the bundle and Fulcio certificate to extract provenance fields.

    Returns a dict with keys:
        image_digest, rekor_log_index, rekor_entry_url,
        operator_identity, oidc_issuer, signed_at

    If ``bundle_dest`` is given, the cosign bundle is copied there for later
    use by ``contained verify``.

    Raises ``RuntimeError`` on cosign failure.
    """
    from contained.docker_runner import _find_cosign, _find_docker

    cosign_bin = _find_cosign()
    docker_bin = _find_docker()
    image_id = _get_image_id(docker_bin, image)

    with tempfile.TemporaryDirectory() as tmp:
        payload_path = Path(tmp) / "digest.txt"
        bundle_path = Path(tmp) / "bundle.json"

        payload_path.write_text(image_id)

        # stderr is NOT captured — cosign prints the OIDC browser/device-flow
        # URL there, and the operator must see it to complete authentication.
        # --output-certificate is intentionally omitted: newer cosign versions
        # (v2.4+) use the new bundle format by default and ignore that flag.
        # The certificate is read directly from the bundle JSON instead.
        result = subprocess.run(
            [
                cosign_bin,
                "sign-blob",
                "--yes",
                f"--rekor-url={rekor_url}",
                f"--fulcio-url={fulcio_url}",
                "--bundle",
                str(bundle_path),
                str(payload_path),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError("cosign signing failed — see output above")

        bundle_text = bundle_path.read_text()
        bundle = json.loads(bundle_text)

        if bundle_dest is not None:
            bundle_dest.parent.mkdir(parents=True, exist_ok=True)
            bundle_dest.write_text(bundle_text)

    # Parse Rekor metadata from bundle
    tlog = bundle["verificationMaterial"]["tlogEntries"][0]
    log_index = int(tlog["logIndex"])
    integrated_time = int(tlog["integratedTime"])
    signed_at = datetime.fromtimestamp(integrated_time, tz=timezone.utc).isoformat()
    rekor_entry_url = f"{rekor_url}/api/v1/log/entries?logIndex={log_index}"

    # Extract the Fulcio certificate from the bundle and parse identity/issuer.
    # rawBytes is base64-encoded DER; convert to PEM for openssl parsing.
    import base64
    import ssl as _ssl

    cert_der = base64.b64decode(bundle["verificationMaterial"]["certificate"]["rawBytes"])
    cert_pem = _ssl.DER_cert_to_PEM_cert(cert_der)
    identity, oidc_issuer = _parse_fulcio_cert(cert_pem)

    return {
        "image_digest": image_id,
        "rekor_log_index": log_index,
        "rekor_entry_url": rekor_entry_url,
        "operator_identity": identity,
        "oidc_issuer": oidc_issuer,
        "signed_at": signed_at,
    }
