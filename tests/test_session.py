"""
Unit tests for contained.session — pure-logic functions that run without Docker.

Excluded: start_repl (delegates to DockerRunner or spawns claude subprocess),
          _print_splash / _print_runtime_banner (Rich console output only).
"""

import yaml

from contained.session import (
    _check_initialised,
    _get_tracer,
    _load_manifest,
    _load_model_config,
)

# ── _load_manifest ────────────────────────────────────────────────────────────


class TestLoadManifest:
    def test_returns_empty_dict_when_no_manifest(self, tmp_path):
        result = _load_manifest(tmp_path)
        assert result == {}

    def test_loads_new_path(self, tmp_path):
        manifest_dir = tmp_path / ".contAIned"
        manifest_dir.mkdir()
        (manifest_dir / "manifest.yaml").write_text("agent:\n  model: claude-opus-4-6\n")
        result = _load_manifest(tmp_path)
        assert result["agent"]["model"] == "claude-opus-4-6"

    def test_falls_back_to_legacy_path(self, tmp_path):
        legacy_dir = tmp_path / ".contAIned" / "policy"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "manifest.yaml").write_text("agent:\n  model: claude-haiku-4-5\n")
        result = _load_manifest(tmp_path)
        assert result["agent"]["model"] == "claude-haiku-4-5"

    def test_new_path_takes_precedence_over_legacy(self, tmp_path):
        new_dir = tmp_path / ".contAIned"
        new_dir.mkdir()
        (new_dir / "manifest.yaml").write_text("agent:\n  model: new-model\n")
        legacy_dir = new_dir / "policy"
        legacy_dir.mkdir()
        (legacy_dir / "manifest.yaml").write_text("agent:\n  model: old-model\n")
        result = _load_manifest(tmp_path)
        assert result["agent"]["model"] == "new-model"

    def test_returns_empty_dict_for_empty_yaml_file(self, tmp_path):
        manifest_dir = tmp_path / ".contAIned"
        manifest_dir.mkdir()
        (manifest_dir / "manifest.yaml").write_text("")
        result = _load_manifest(tmp_path)
        assert result == {}

    def test_returns_full_manifest_structure(self, tmp_path):
        manifest_dir = tmp_path / ".contAIned"
        manifest_dir.mkdir()
        data = {
            "runtime": {"docker": {"image": "contained:latest"}},
            "policy": {"egress": {"enabled": True}},
            "agent": {"model": "claude-sonnet-4-6"},
        }
        (manifest_dir / "manifest.yaml").write_text(yaml.dump(data))
        result = _load_manifest(tmp_path)
        assert result["runtime"]["docker"]["image"] == "contained:latest"
        assert result["policy"]["egress"]["enabled"] is True


# ── _load_model_config ────────────────────────────────────────────────────────


class TestLoadModelConfig:
    def test_returns_model_string(self, tmp_path):
        manifest_dir = tmp_path / ".contAIned"
        manifest_dir.mkdir()
        (manifest_dir / "manifest.yaml").write_text("agent:\n  model: claude-sonnet-4-6\n")
        assert _load_model_config(tmp_path) == "claude-sonnet-4-6"

    def test_returns_none_when_no_manifest(self, tmp_path):
        assert _load_model_config(tmp_path) is None

    def test_returns_none_when_agent_section_missing(self, tmp_path):
        manifest_dir = tmp_path / ".contAIned"
        manifest_dir.mkdir()
        (manifest_dir / "manifest.yaml").write_text("policy:\n  egress:\n    enabled: true\n")
        assert _load_model_config(tmp_path) is None

    def test_returns_none_when_model_key_missing(self, tmp_path):
        manifest_dir = tmp_path / ".contAIned"
        manifest_dir.mkdir()
        (manifest_dir / "manifest.yaml").write_text("agent:\n  other_key: value\n")
        assert _load_model_config(tmp_path) is None

    def test_returns_none_when_model_is_empty_string(self, tmp_path):
        manifest_dir = tmp_path / ".contAIned"
        manifest_dir.mkdir()
        (manifest_dir / "manifest.yaml").write_text("agent:\n  model: ''\n")
        assert _load_model_config(tmp_path) is None


# ── _check_initialised ────────────────────────────────────────────────────────


class TestCheckInitialised:
    def _scaffold(self, root):
        """Create the minimum file set that _check_initialised expects."""
        hooks = root / ".contAIned" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "restrict_writes.py").touch()
        (hooks / "audit.py").touch()
        (hooks / "qa.py").touch()
        (root / ".contAIned" / "manifest.yaml").touch()

    def test_returns_empty_when_fully_initialised(self, tmp_path):
        self._scaffold(tmp_path)
        assert _check_initialised(tmp_path) == []

    def test_returns_missing_hooks(self, tmp_path):
        self._scaffold(tmp_path)
        (tmp_path / ".contAIned" / "hooks" / "restrict_writes.py").unlink()
        (tmp_path / ".contAIned" / "hooks" / "qa.py").unlink()
        missing = _check_initialised(tmp_path)
        assert ".contAIned/hooks/restrict_writes.py" in missing
        assert ".contAIned/hooks/qa.py" in missing

    def test_accepts_legacy_manifest_path(self, tmp_path):
        """Legacy manifest at .contAIned/policy/manifest.yaml should satisfy check."""
        self._scaffold(tmp_path)
        # Remove new-path manifest, create legacy one
        (tmp_path / ".contAIned" / "manifest.yaml").unlink()
        legacy_dir = tmp_path / ".contAIned" / "policy"
        legacy_dir.mkdir()
        (legacy_dir / "manifest.yaml").touch()
        missing = _check_initialised(tmp_path)
        assert ".contAIned/manifest.yaml" not in missing

    def test_reports_missing_manifest_when_neither_path_exists(self, tmp_path):
        self._scaffold(tmp_path)
        (tmp_path / ".contAIned" / "manifest.yaml").unlink()
        missing = _check_initialised(tmp_path)
        assert ".contAIned/manifest.yaml" in missing

    def test_returns_all_missing_on_empty_directory(self, tmp_path):
        missing = _check_initialised(tmp_path)
        assert len(missing) == 4  # 3 hooks + manifest

    def test_missing_paths_are_relative_to_root(self, tmp_path):
        missing = _check_initialised(tmp_path)
        for m in missing:
            assert not m.startswith("/"), f"Expected relative path, got: {m}"


# ── _get_tracer ───────────────────────────────────────────────────────────────


class TestGetTracer:
    def test_returns_tracer_instance(self, tmp_path):
        from contained.tracer import contAInedTracer

        (tmp_path / ".contAIned").mkdir()
        tracer = _get_tracer(tmp_path)
        assert tracer is not None
        assert isinstance(tracer, contAInedTracer)

    def test_creates_db_at_expected_path(self, tmp_path):
        (tmp_path / ".contAIned").mkdir()
        _get_tracer(tmp_path)
        expected = tmp_path / ".contAIned" / "tracer.db"
        assert expected.exists()

    def test_returns_none_gracefully_on_error(self, tmp_path, monkeypatch):
        """If tracer construction raises, _get_tracer returns None."""
        import contained.session as session_module

        # Patch contAInedTracer to raise on construction
        import contained.tracer as tracer_mod

        original = tracer_mod.contAInedTracer

        class BrokenTracer:
            def __init__(self, *a, **kw):
                raise RuntimeError("simulated failure")

        monkeypatch.setattr(tracer_mod, "contAInedTracer", BrokenTracer)
        # Clear any cached import so the patched class is used
        import importlib

        importlib.reload(session_module)
        result = session_module._get_tracer(tmp_path)
        assert result is None
        # Restore
        monkeypatch.setattr(tracer_mod, "contAInedTracer", original)
        importlib.reload(session_module)
