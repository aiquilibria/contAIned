"""
Unit tests for contained.init — pure-logic functions that run without Docker.

Excluded: _docker_setup, run_init (require Docker or interactive prompts).
"""

import stat
from unittest.mock import patch

import yaml

from contained.docker_runner import _find_cosign
from contained.init import (
    _build_managed_settings,
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
    _write_provenance,
)
from contained.sigstore import _extract_oidc_issuer, _extract_san
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
        assert qa == {
            "syntax": True,
            "lint": True,
            "format": True,
            "type": True,
            "test": True,
            "coverage": True,
            "coverage_threshold": 80,
        }

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
        data = self._parse(docker_config=None, model="m", network_enabled=True)
        domains = data["policy"]["network"]["allowed_domains"]
        assert "api.anthropic.com" in domains

    def test_extra_network_domains_appended(self):
        data = self._parse(
            docker_config=None,
            model="m",
            network_enabled=True,
            network_extra_domains=["pypi.org", "github.com"],
        )
        domains = data["policy"]["network"]["allowed_domains"]
        assert "pypi.org" in domains
        assert "github.com" in domains

    def test_network_disabled(self):
        data = self._parse(docker_config=None, model="m", network_enabled=False)
        assert data["policy"]["network"]["enabled"] is False

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

    def test_sigstore_enabled_by_default(self):
        data = self._parse(docker_config=None, model="m")
        s = data["sigstore"]
        assert s["enabled"] is True
        assert s["rekor_url"] == "https://rekor.sigstore.dev"
        assert s["fulcio_url"] == "https://fulcio.sigstore.dev"

    def test_sigstore_enabled_writes_urls(self):
        data = self._parse(docker_config=None, model="m", sigstore_enabled=True)
        s = data["sigstore"]
        assert s["enabled"] is True
        assert s["rekor_url"] == "https://rekor.sigstore.dev"
        assert s["fulcio_url"] == "https://fulcio.sigstore.dev"

    def test_sigstore_disabled_omits_urls(self):
        data = self._parse(docker_config=None, model="m", sigstore_enabled=False)
        s = data["sigstore"]
        assert "rekor_url" not in s
        assert "fulcio_url" not in s


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

    def test_settings_json_not_present(self, tmp_path):
        entries = _managed_files(tmp_path)
        settings = [e for e in entries if "settings.json" in str(e[0])]
        assert len(settings) == 0

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


# ── _find_cosign ──────────────────────────────────────────────────────────────


class TestFindCosign:
    def test_finds_cosign_on_path(self, tmp_path):
        fake = tmp_path / "cosign"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        with patch("shutil.which", return_value=str(fake)):
            result = _find_cosign()
        assert result == str(fake)

    def test_finds_cosign_at_known_path(self, tmp_path):
        fake = tmp_path / "cosign"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        with patch("shutil.which", return_value=None):
            with patch("contained.docker_runner._COSIGN_SEARCH_PATHS", [str(fake)]):
                result = _find_cosign()
        assert result == str(fake)

    def test_raises_when_not_found(self):
        with patch("shutil.which", return_value=None):
            with patch("contained.docker_runner._COSIGN_SEARCH_PATHS", []):
                try:
                    _find_cosign()
                    assert False, "expected FileNotFoundError"
                except FileNotFoundError as exc:
                    assert "cosign" in str(exc).lower()

    def test_error_message_contains_install_hint(self):
        with patch("shutil.which", return_value=None):
            with patch("contained.docker_runner._COSIGN_SEARCH_PATHS", []):
                try:
                    _find_cosign()
                except FileNotFoundError as exc:
                    assert "sigstore.dev" in str(exc)


# ── _extract_san ──────────────────────────────────────────────────────────────


class TestExtractSan:
    def test_extracts_email(self):
        output = "X509v3 Subject Alternative Name:\n    email:user@example.com\n"
        assert _extract_san(output) == "user@example.com"

    def test_extracts_uri(self):
        output = "X509v3 Subject Alternative Name:\n    URI:https://github.com/actions\n"
        assert _extract_san(output) == "https://github.com/actions"

    def test_extracts_email_from_comma_separated(self):
        output = "    email:user@example.com, URI:https://accounts.google.com\n"
        assert _extract_san(output) == "user@example.com"

    def test_returns_unknown_when_no_san(self):
        assert _extract_san("no relevant content here") == "unknown"


# ── _extract_oidc_issuer ──────────────────────────────────────────────────────


class TestExtractOidcIssuer:
    def test_extracts_issuer_after_oid(self):
        output = "            1.3.6.1.4.1.57264.1.1:\n                https://accounts.google.com\n"
        assert _extract_oidc_issuer(output) == "https://accounts.google.com"

    def test_extracts_github_issuer(self):
        output = (
            "            1.3.6.1.4.1.57264.1.1:\n"
            "                https://token.actions.githubusercontent.com\n"
        )
        assert _extract_oidc_issuer(output) == "https://token.actions.githubusercontent.com"

    def test_returns_unknown_when_oid_absent(self):
        assert _extract_oidc_issuer("no OID here") == "unknown"


# ── _write_provenance ─────────────────────────────────────────────────────────


class TestWriteProvenance:
    _DATA = {
        "image_digest": "sha256:abc123",
        "rekor_log_index": 42,
        "rekor_entry_url": "https://rekor.sigstore.dev/api/v1/log/entries?logIndex=42",
        "operator_identity": "user@example.com",
        "oidc_issuer": "https://accounts.google.com",
        "signed_at": "2026-03-20T12:00:00+00:00",
    }

    def test_creates_provenance_yaml(self, tmp_path):
        (tmp_path / ".contAIned").mkdir()
        status = _write_provenance(tmp_path, self._DATA)
        assert status == "created"
        prov_path = tmp_path / ".contAIned" / "provenance.yaml"
        assert prov_path.exists()

    def test_schema_version_is_1(self, tmp_path):
        (tmp_path / ".contAIned").mkdir()
        _write_provenance(tmp_path, self._DATA)
        data = yaml.safe_load((tmp_path / ".contAIned" / "provenance.yaml").read_text())
        assert data["schema_version"] == 1

    def test_all_fields_written(self, tmp_path):
        (tmp_path / ".contAIned").mkdir()
        _write_provenance(tmp_path, self._DATA)
        data = yaml.safe_load((tmp_path / ".contAIned" / "provenance.yaml").read_text())
        for key in self._DATA:
            assert key in data

    def test_overwrites_on_reinit(self, tmp_path):
        (tmp_path / ".contAIned").mkdir()
        _write_provenance(tmp_path, self._DATA)
        updated = {**self._DATA, "rekor_log_index": 99}
        _write_provenance(tmp_path, updated)
        data = yaml.safe_load((tmp_path / ".contAIned" / "provenance.yaml").read_text())
        assert data["rekor_log_index"] == 99


# ── _build_manifest (mcp / skills) ───────────────────────────────────────────


class TestBuildManifestMcpSkills:
    def _parse(self, **kwargs) -> dict:
        return yaml.safe_load(_build_manifest(**kwargs))

    def test_mcp_approved_servers_in_manifest(self):
        data = self._parse(
            docker_config=None,
            model="m",
            mcp_approved_servers=["github", "puppeteer"],
        )
        assert data["policy"]["mcp"]["approved_servers"] == ["github", "puppeteer"]

    def test_approved_skills_in_manifest(self):
        data = self._parse(
            docker_config=None,
            model="m",
            approved_skills=["commit", "review-pr"],
        )
        assert data["policy"]["skills"]["approved_skills"] == ["commit", "review-pr"]

    def test_empty_mcp_and_skills_by_default(self):
        data = self._parse(docker_config=None, model="m")
        assert data["policy"]["mcp"]["approved_servers"] == []
        assert data["policy"]["skills"]["approved_skills"] == []


# ── _build_managed_settings ───────────────────────────────────────────────────


import json  # noqa: E402


class TestBuildManagedSettings:
    def _settings(self, manifest: dict | None = None) -> dict:
        return json.loads(_build_managed_settings(manifest or {}))

    def test_returns_valid_json(self):
        result = _build_managed_settings({})
        data = json.loads(result)
        assert isinstance(data, dict)

    def test_default_domains_in_allow_rules(self):
        data = self._settings()
        allow = data["permissions"]["allow"]
        assert "WebFetch(domain:api.anthropic.com)" in allow
        assert "WebFetch(domain:code.claude.com)" in allow
        assert "WebFetch(domain:docs.anthropic.com)" in allow

    def test_extra_domain_generates_allow_rule(self):
        manifest = {
            "policy": {
                "network": {
                    "allowed_domains": ["api.anthropic.com", "pypi.org"],
                }
            }
        }
        data = self._settings(manifest)
        assert "WebFetch(domain:pypi.org)" in data["permissions"]["allow"]

    def test_ask_rules_include_webfetch_and_websearch(self):
        data = self._settings()
        ask = data["permissions"]["ask"]
        assert "WebFetch" in ask
        assert "WebSearch" in ask

    def test_sandbox_allowed_domains_matches_network_policy(self):
        manifest = {
            "policy": {
                "network": {
                    "allowed_domains": ["api.anthropic.com", "example.com"],
                }
            }
        }
        data = self._settings(manifest)
        assert data["sandbox"]["network"]["allowedDomains"] == ["api.anthropic.com", "example.com"]

    def test_mcp_server_generates_allow_rule(self):
        manifest = {"policy": {"mcp": {"approved_servers": ["github", "puppeteer"]}}}
        data = self._settings(manifest)
        allow = data["permissions"]["allow"]
        assert "mcp__github__*" in allow
        assert "mcp__puppeteer__*" in allow

    def test_mcp_servers_in_allowed_mcp_servers(self):
        manifest = {"policy": {"mcp": {"approved_servers": ["github"]}}}
        data = self._settings(manifest)
        names = [entry["serverName"] for entry in data["allowedMcpServers"]]
        assert "github" in names

    def test_no_allowed_mcp_servers_key_when_empty(self):
        data = self._settings()
        assert "allowedMcpServers" not in data

    def test_skill_generates_allow_rule(self):
        manifest = {"policy": {"skills": {"approved_skills": ["commit", "review-pr"]}}}
        data = self._settings(manifest)
        allow = data["permissions"]["allow"]
        assert "Skill(commit)" in allow
        assert "Skill(review-pr)" in allow

    def test_workspace_read_rules_always_present(self):
        data = self._settings()
        allow = data["permissions"]["allow"]
        assert "Read(/workspace/**)" in allow
        assert "Glob(/workspace/**)" in allow
        assert "Grep(/workspace/**)" in allow

    def test_sandbox_enabled(self):
        data = self._settings()
        assert data["sandbox"]["enabled"] is True
        assert data["sandbox"]["enableWeakerNestedSandbox"] is True
