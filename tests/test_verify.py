"""
Unit tests for contained.verify — pure-logic paths that don't need Docker or cosign.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from contained.verify import run_verify


def _make_workspace(tmp_path: Path, *, sigstore_enabled: bool = True) -> Path:
    """Create a minimal workspace with manifest and provenance files."""
    (tmp_path / ".contAIned").mkdir()

    manifest = {
        "sigstore": {
            "enabled": sigstore_enabled,
            "rekor_url": "https://rekor.sigstore.dev",
            "fulcio_url": "https://fulcio.sigstore.dev",
        },
        "runtime": {"docker": {"image": "contained:latest"}},
    }
    (tmp_path / ".contAIned" / "manifest.yaml").write_text(
        yaml.dump(manifest, default_flow_style=False)
    )
    return tmp_path


def _add_provenance(ws: Path, digest: str = "sha256:abc123") -> None:
    provenance = {
        "schema_version": 1,
        "image_digest": digest,
        "rekor_log_index": 42,
        "rekor_entry_url": "https://rekor.sigstore.dev/api/v1/log/entries?logIndex=42",
        "operator_identity": "user@example.com",
        "oidc_issuer": "https://accounts.google.com",
        "signed_at": "2026-03-20T12:00:00+00:00",
    }
    (ws / ".contAIned" / "provenance.yaml").write_text(
        yaml.dump(provenance, default_flow_style=False)
    )
    # minimal bundle placeholder (verify mocks cosign_verify_blob)
    (ws / ".contAIned" / "provenance.bundle").write_text("{}")


class TestRunVerifyDisabled:
    def test_exits_0_when_sigstore_disabled(self, tmp_path):
        ws = _make_workspace(tmp_path, sigstore_enabled=False)
        # Should complete without raising SystemExit (disabled is not an error)
        run_verify(ws)

    def test_exits_1_when_no_manifest(self, tmp_path):
        (tmp_path / ".contAIned").mkdir()
        with pytest.raises(SystemExit) as exc:
            run_verify(tmp_path)
        assert exc.value.code == 1


class TestRunVerifyMissingFiles:
    def test_exits_1_when_no_provenance_yaml(self, tmp_path):
        _make_workspace(tmp_path)
        with pytest.raises(SystemExit) as exc:
            run_verify(tmp_path)
        assert exc.value.code == 1

    def test_exits_1_when_no_bundle(self, tmp_path):
        ws = _make_workspace(tmp_path)
        _add_provenance(ws)
        (ws / ".contAIned" / "provenance.bundle").unlink()
        with (
            patch("contained.verify._find_docker", return_value="/usr/bin/docker"),
            patch("contained.verify._get_image_id", return_value="sha256:abc123"),
            patch("contained.verify._find_cosign", return_value="/usr/bin/cosign"),
            pytest.raises(SystemExit) as exc,
        ):
            run_verify(ws)
        assert exc.value.code == 1


class TestRunVerifyDigestMismatch:
    def test_exits_1_on_digest_mismatch(self, tmp_path):
        ws = _make_workspace(tmp_path)
        _add_provenance(ws, digest="sha256:abc123")
        with (
            patch("contained.verify._find_docker", return_value="/usr/bin/docker"),
            patch("contained.verify._get_image_id", return_value="sha256:different"),
            pytest.raises(SystemExit) as exc,
        ):
            run_verify(ws)
        assert exc.value.code == 1


class TestRunVerifySuccess:
    def test_exits_0_on_valid_provenance(self, tmp_path):
        ws = _make_workspace(tmp_path)
        _add_provenance(ws, digest="sha256:abc123")
        with (
            patch("contained.verify._find_docker", return_value="/usr/bin/docker"),
            patch("contained.verify._get_image_id", return_value="sha256:abc123"),
            patch("contained.verify._find_cosign", return_value="/usr/bin/cosign"),
            patch("contained.verify.cosign_verify_blob"),  # success = no exception
        ):
            # Should complete without raising SystemExit
            run_verify(ws)

    def test_exits_1_on_cosign_failure(self, tmp_path):
        ws = _make_workspace(tmp_path)
        _add_provenance(ws, digest="sha256:abc123")
        with (
            patch("contained.verify._find_docker", return_value="/usr/bin/docker"),
            patch("contained.verify._get_image_id", return_value="sha256:abc123"),
            patch("contained.verify._find_cosign", return_value="/usr/bin/cosign"),
            patch(
                "contained.verify.cosign_verify_blob",
                side_effect=RuntimeError("signature invalid"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            run_verify(ws)
        assert exc.value.code == 1
