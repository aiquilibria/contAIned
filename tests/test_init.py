"""
Unit tests for contained.init — pure-logic functions that run without Docker.

Excluded: _docker_setup, run_init (require Docker or interactive prompts).
"""

import stat

import yaml

from contained.init import (
    _build_manifest,
    _contAIned_version,
    _git_root,
    _init_git_repo,
    _is_git_repo,
    _managed_files,
    _sync_manifest,
    _touch,
    _update_gitignore,
    _write_file,
)
from contained.templates import GITIGNORE_BLOCK, GITIGNORE_TEMPLATE

# ── _write_file ───────────────────────────────────────────────────────────────


class TestWriteFile:
    def test_creates_new_file(self, tmp_path):
        p = tmp_path / "foo.txt"
        result = _write_file(p, "hello")
        assert result == "created"
        assert p.read_text() == "hello"

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "c.txt"
        result = _write_file(p, "content")
        assert result == "created"
        assert p.exists()

    def test_existing_no_overwrite_returns_exists(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("original")
        result = _write_file(p, "new content")
        assert result == "exists"
        assert p.read_text() == "original"  # unchanged

    def test_existing_overwrite_different_content_returns_updated(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("old")
        result = _write_file(p, "new", overwrite=True)
        assert result == "updated"
        assert p.read_text() == "new"

    def test_existing_overwrite_identical_content_returns_exists(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("same")
        result = _write_file(p, "same", overwrite=True)
        assert result == "exists"

    def test_sets_executable_bit_on_new_file(self, tmp_path):
        p = tmp_path / "script.py"
        _write_file(p, "#!/usr/bin/env python3", executable=True)
        mode = p.stat().st_mode
        assert mode & stat.S_IXUSR
        assert mode & stat.S_IXGRP
        assert mode & stat.S_IXOTH

    def test_sets_executable_bit_on_overwrite(self, tmp_path):
        p = tmp_path / "script.py"
        p.write_text("old content")
        _write_file(p, "new content", executable=True, overwrite=True)
        mode = p.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_no_executable_bit_by_default(self, tmp_path):
        p = tmp_path / "data.txt"
        _write_file(p, "data")
        mode = p.stat().st_mode
        # Should not be executable (owner exec bit)
        assert not (mode & stat.S_IXUSR)


# ── _touch ────────────────────────────────────────────────────────────────────


class TestTouch:
    def test_creates_empty_file(self, tmp_path):
        p = tmp_path / ".gitkeep"
        result = _touch(p)
        assert result == "created"
        assert p.exists()
        assert p.read_text() == ""

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "deep" / "dir" / ".gitkeep"
        _touch(p)
        assert p.exists()

    def test_idempotent_returns_exists(self, tmp_path):
        p = tmp_path / ".gitkeep"
        p.touch()
        result = _touch(p)
        assert result == "exists"


# ── _is_git_repo / _git_root ──────────────────────────────────────────────────


class TestGitDetection:
    def test_is_git_repo_true_at_root(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert _is_git_repo(tmp_path) is True

    def test_is_git_repo_true_in_subdirectory(self, tmp_path):
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        assert _is_git_repo(subdir) is True

    def test_is_git_repo_false_no_git(self, tmp_path):
        assert _is_git_repo(tmp_path) is False

    def test_git_root_returns_root_path(self, tmp_path):
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src" / "pkg"
        subdir.mkdir(parents=True)
        assert _git_root(subdir) == tmp_path.resolve()

    def test_git_root_returns_none_outside_repo(self, tmp_path):
        assert _git_root(tmp_path) is None

    def test_git_root_at_root_returns_root(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert _git_root(tmp_path) == tmp_path.resolve()


# ── _init_git_repo ────────────────────────────────────────────────────────────


class TestInitGitRepo:
    def test_creates_new_repo(self, tmp_path):
        result = _init_git_repo(tmp_path)
        assert result == "created"
        assert (tmp_path / ".git").exists()

    def test_detects_existing_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        result = _init_git_repo(tmp_path)
        assert result == "exists"

    def test_existing_repo_not_reinitialised(self, tmp_path):
        # Place a sentinel file inside .git to verify it isn't blown away
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        sentinel = git_dir / "sentinel"
        sentinel.write_text("marker")
        _init_git_repo(tmp_path)
        assert sentinel.exists()


# ── _update_gitignore ─────────────────────────────────────────────────────────


class TestUpdateGitignore:
    def test_creates_template_when_no_file(self, tmp_path):
        result = _update_gitignore(tmp_path)
        assert result == "created"
        content = (tmp_path / ".gitignore").read_text()
        assert content == GITIGNORE_TEMPLATE

    def test_already_configured_with_trailing_slash(self, tmp_path):
        gi = tmp_path / ".gitignore"
        gi.write_text(".contAIned/\n")
        result = _update_gitignore(tmp_path)
        assert result == "already configured"
        assert gi.read_text() == ".contAIned/\n"  # unchanged

    def test_already_configured_without_trailing_slash(self, tmp_path):
        gi = tmp_path / ".gitignore"
        gi.write_text(".contAIned\n")
        result = _update_gitignore(tmp_path)
        assert result == "already configured"

    def test_upgrades_old_audit_block(self, tmp_path):
        gi = tmp_path / ".gitignore"
        old_content = "# contAIned —\n.contAIned/audit/\n"
        gi.write_text(old_content)
        result = _update_gitignore(tmp_path)
        assert result == "updated"
        new_content = gi.read_text()
        assert ".contAIned/" in new_content
        assert ".contAIned/audit/" not in new_content

    def test_appends_block_when_no_contAIned_section(self, tmp_path):
        gi = tmp_path / ".gitignore"
        gi.write_text("# existing\n*.pyc\n")
        result = _update_gitignore(tmp_path)
        assert result == "updated"
        content = gi.read_text()
        assert "# existing\n*.pyc\n" in content  # original preserved
        assert GITIGNORE_BLOCK in content  # block appended


# ── _sync_manifest ────────────────────────────────────────────────────────────


class TestSyncManifest:
    TEMPLATE = "a: 1\nb: 2\nc:\n  x: 10\n  y: 20\n"

    def test_creates_file_when_missing(self, tmp_path):
        p = tmp_path / "manifest.yaml"
        result = _sync_manifest(p, self.TEMPLATE)
        assert result == "created"
        assert p.exists()

    def test_no_change_when_content_matches_template(self, tmp_path):
        p = tmp_path / "manifest.yaml"
        p.write_text(self.TEMPLATE)
        result = _sync_manifest(p, self.TEMPLATE)
        assert result == "exists"

    def test_adds_missing_keys_from_template(self, tmp_path):
        p = tmp_path / "manifest.yaml"
        p.write_text("a: 99\n")  # missing b and c
        result = _sync_manifest(p, self.TEMPLATE)
        assert result == "updated"
        data = yaml.safe_load(p.read_text())
        assert data["b"] == 2  # filled from template
        assert data["c"] == {"x": 10, "y": 20}

    def test_preserves_existing_values(self, tmp_path):
        p = tmp_path / "manifest.yaml"
        p.write_text("a: 99\nb: 77\nc:\n  x: 5\n  y: 6\n")
        _sync_manifest(p, self.TEMPLATE)
        data = yaml.safe_load(p.read_text())
        assert data["a"] == 99  # user value preserved
        assert data["b"] == 77

    def test_drops_keys_not_in_template(self, tmp_path):
        p = tmp_path / "manifest.yaml"
        p.write_text("a: 1\nb: 2\nc:\n  x: 10\n  y: 20\nlegacy_key: old\n")
        _sync_manifest(p, self.TEMPLATE)
        data = yaml.safe_load(p.read_text())
        assert "legacy_key" not in data

    def test_recursive_dict_merge(self, tmp_path):
        template = "parent:\n  child_a: 1\n  child_b: 2\n"
        p = tmp_path / "manifest.yaml"
        p.write_text("parent:\n  child_a: 99\n")  # child_b missing
        _sync_manifest(p, template)
        data = yaml.safe_load(p.read_text())
        assert data["parent"]["child_a"] == 99  # preserved
        assert data["parent"]["child_b"] == 2  # filled

    def test_leaves_unparseable_file_untouched(self, tmp_path):
        p = tmp_path / "manifest.yaml"
        garbage = "{{not: valid: yaml: [\n"
        p.write_text(garbage)
        result = _sync_manifest(p, self.TEMPLATE)
        assert result == "exists"
        assert p.read_text() == garbage


# ── _build_manifest ───────────────────────────────────────────────────────────


class TestBuildManifest:
    def _parse(self, **kwargs) -> dict:
        return yaml.safe_load(_build_manifest(**kwargs))

    def test_returns_valid_yaml_string(self):
        result = _build_manifest(docker_config=None, model="claude-sonnet-4-6")
        assert isinstance(result, str)
        data = yaml.safe_load(result)
        assert isinstance(data, dict)

    def test_model_placed_in_agent_section(self):
        data = self._parse(docker_config=None, model="claude-opus-4-6")
        assert data["agent"]["model"] == "claude-opus-4-6"

    def test_default_qa_all_true(self):
        data = self._parse(docker_config=None, model="m")
        qa = data["policy"]["qa"]
        assert qa == {"syntax": True, "lint": True, "format": True, "type": True}

    def test_qa_choices_override_defaults(self):
        data = self._parse(
            docker_config=None,
            model="m",
            qa_choices={"lint": False, "type": False},
        )
        qa = data["policy"]["qa"]
        assert qa["lint"] is False
        assert qa["type"] is False
        assert qa["syntax"] is True  # default kept

    def test_anthropic_always_in_allowed_domains(self):
        data = self._parse(docker_config=None, model="m", egress_enabled=True)
        domains = data["policy"]["egress"]["allowed_domains"]
        assert "api.anthropic.com" in domains

    def test_extra_egress_domains_appended(self):
        data = self._parse(
            docker_config=None,
            model="m",
            egress_enabled=True,
            egress_extra_domains=["pypi.org", "github.com"],
        )
        domains = data["policy"]["egress"]["allowed_domains"]
        assert "pypi.org" in domains
        assert "github.com" in domains

    def test_egress_disabled(self):
        data = self._parse(docker_config=None, model="m", egress_enabled=False)
        assert data["policy"]["egress"]["enabled"] is False

    def test_docker_config_written_to_runtime(self):
        docker_cfg = {
            "image": "myimage:latest",
            "memory": "4g",
            "cpus": 4,
            "network": "mynet",
            "agent_config_volume": "myvol",
        }
        data = self._parse(docker_config=docker_cfg, model="m")
        rt = data["runtime"]["docker"]
        assert rt["image"] == "myimage:latest"
        assert rt["memory"] == "4g"
        assert rt["cpus"] == 4

    def test_no_docker_config_leaves_runtime_empty(self):
        data = self._parse(docker_config=None, model="m")
        assert data["runtime"] == {}


# ── _managed_files ────────────────────────────────────────────────────────────


class TestManagedFiles:
    def test_returns_list_of_tuples(self, tmp_path):
        entries = _managed_files(tmp_path)
        assert isinstance(entries, list)
        assert all(isinstance(e, tuple) and len(e) == 3 for e in entries)

    def test_all_paths_under_target(self, tmp_path):
        entries = _managed_files(tmp_path)
        for path, _content, _exe in entries:
            assert str(path).startswith(str(tmp_path))

    def test_hooks_are_executable(self, tmp_path):
        entries = _managed_files(tmp_path)
        # _policy.py is a shared module (imported, not run directly) — not executable.
        # All other hook scripts should be executable.
        hook_entries = [
            (p, exe)
            for p, _c, exe in entries
            if ".contAIned/hooks" in str(p) and "_policy.py" not in str(p)
        ]
        assert len(hook_entries) > 0
        for path, exe in hook_entries:
            assert exe is True, f"{path} should be executable"

    def test_policy_module_not_executable(self, tmp_path):
        entries = _managed_files(tmp_path)
        policy = [e for e in entries if "_policy.py" in str(e[0])]
        assert len(policy) == 1
        assert policy[0][2] is False

    def test_settings_json_not_executable(self, tmp_path):
        entries = _managed_files(tmp_path)
        settings = [e for e in entries if "settings.json" in str(e[0])]
        assert len(settings) == 1
        assert settings[0][2] is False

    def test_settings_json_contains_workspace_path(self, tmp_path):
        entries = _managed_files(tmp_path)
        settings = next(e for e in entries if "settings.json" in str(e[0]))
        assert str(tmp_path.resolve()) in settings[1]

    def test_claude_md_present(self, tmp_path):
        entries = _managed_files(tmp_path)
        paths = [str(e[0]) for e in entries]
        assert any("CLAUDE.md" in p for p in paths)


# ── _contAIned_version ────────────────────────────────────────────────────────


class TestContainedVersion:
    def test_returns_string(self):
        result = _contAIned_version()
        assert isinstance(result, str)

    def test_never_raises(self):
        # Should always return something — either version string or "unknown"
        result = _contAIned_version()
        assert result  # non-empty

    def test_returns_known_version_or_unknown(self):
        result = _contAIned_version()
        # Either a semver-like string or the fallback
        assert result == "unknown" or result[0].isdigit() or result == "0.1.0"
