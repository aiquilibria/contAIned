"""
Tests for the generic QA hook (qa.checks) logic — phase 3 of the
generic-qa-and-policy-generalization plan.

Covers:
  3.7  a checks list with a single non-Python entry runs only that command
  3.8  a check with when_changed: ["*.ts"] is skipped when no .ts files touched
  3.9  empty checks list passes QA trivially (no block decision emitted)

The _expand and _files_match helpers live inside the QA_HOOK template string
and cannot be imported directly.  Tests are written as self-contained functions
that mirror the logic, plus one subprocess integration test that writes the
hook to a temp dir and invokes it with a crafted policy.
"""

import fnmatch
import json
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).parent.parent / "cli" / "internal" / "scaffold" / "templates" / "hooks"
POLICY_LOADER_HOOK = (_HOOKS_DIR / "_policy.py").read_text()
QA_HOOK = (_HOOKS_DIR / "qa.py").read_text()

# ── Inline mirror of _expand / _files_match (keeps tests independent) ─────────


def _expand(entry) -> dict:
    if isinstance(entry, list):
        return {"name": entry[0], "command": entry, "when_changed": []}
    return {
        "name": entry.get("name", (entry.get("command") or ["?"])[0]),
        "command": entry.get("command", []),
        "when_changed": entry.get("when_changed", []),
    }


def _files_match(touched: list[str] | None, patterns: list[str]) -> bool:
    if touched is None:
        return True
    return any(fnmatch.fnmatch(Path(f).name, pat) for f in touched for pat in patterns)


# ── _expand ───────────────────────────────────────────────────────────────────


class TestExpand:
    def test_bare_array_infers_name_from_command0(self):
        result = _expand(["go", "vet", "./..."])
        assert result["name"] == "go"
        assert result["command"] == ["go", "vet", "./..."]
        assert result["when_changed"] == []

    def test_named_object_preserved(self):
        entry = {"name": "lint", "command": ["ruff", "check", "."], "when_changed": ["*.py"]}
        result = _expand(entry)
        assert result["name"] == "lint"
        assert result["command"] == ["ruff", "check", "."]
        assert result["when_changed"] == ["*.py"]

    def test_named_object_without_when_changed_defaults_to_empty(self):
        entry = {"name": "build", "command": ["make", "build"]}
        result = _expand(entry)
        assert result["when_changed"] == []

    def test_named_object_name_falls_back_to_command0(self):
        entry = {"command": ["pyright"]}
        result = _expand(entry)
        assert result["name"] == "pyright"


# ── _files_match ──────────────────────────────────────────────────────────────


class TestFilesMatch:
    def test_match_by_extension(self):
        touched = ["/workspace/src/foo.py", "/workspace/src/bar.py"]
        assert _files_match(touched, ["*.py"]) is True

    def test_no_match_wrong_extension(self):
        touched = ["/workspace/src/foo.go"]
        assert _files_match(touched, ["*.py"]) is False

    def test_multiple_patterns_any_match(self):
        touched = ["/workspace/src/App.tsx"]
        assert _files_match(touched, ["*.ts", "*.tsx"]) is True

    def test_empty_touched_never_matches(self):
        assert _files_match([], ["*.py"]) is False

    def test_empty_patterns_never_matches(self):
        assert _files_match(["/workspace/foo.py"], []) is False

    def test_none_touched_always_matches(self):
        """None means tracer unavailable — bypass when_changed so checks run."""
        assert _files_match(None, ["*.py"]) is True

    def test_none_touched_matches_any_pattern(self):
        assert _files_match(None, ["*.go"]) is True



# ── Integration: run full QA_HOOK via subprocess ──────────────────────────────


def _write_hook_env(tmp_path: Path) -> tuple[Path, Path]:
    """Write _policy.py and qa.py to tmp_path; return (hooks_dir, manifest_dir)."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()

    manifest_dir = tmp_path / "etc" / "contained"
    manifest_dir.mkdir(parents=True)

    (hooks_dir / "_policy.py").write_text(POLICY_LOADER_HOOK)
    (hooks_dir / "qa.py").write_text(QA_HOOK)
    return hooks_dir, manifest_dir


def _run_hook(hooks_dir: Path, manifest_path: Path, event: dict) -> dict:
    """Run qa.py with the given event JSON and manifest; return parsed JSON output."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(hooks_dir / "qa.py")],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            # Override manifest path via a monkeypatch-friendly env var isn't
            # supported by the hook directly; instead we symlink the manifest
            # into the expected location by writing it to manifest_path and
            # patching the path constant isn't easy in subprocess mode.
            # We work around this by having the test write a _policy.py that
            # hard-codes the manifest path to our temp file.
        },
    )
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout.strip())


def _make_policy_loader(manifest_path: Path) -> str:
    """Return a _policy.py that reads from manifest_path instead of /etc/contained/."""
    src = POLICY_LOADER_HOOK.replace(
        'manifest_path = Path("/etc/contained/manifest.yaml")',
        f"manifest_path = Path({str(manifest_path)!r})",
    )
    return src


def _run_hook_with_manifest(tmp_path: Path, manifest: dict, event: dict) -> dict:
    """Write hook env, manifest, and run qa.py; return parsed stdout JSON."""
    import subprocess

    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)

    manifest_path = tmp_path / "manifest.yaml"
    import yaml

    manifest_path.write_text(yaml.dump(manifest))

    (hooks_dir / "_policy.py").write_text(_make_policy_loader(manifest_path))
    (hooks_dir / "qa.py").write_text(QA_HOOK)

    result = subprocess.run(
        [sys.executable, str(hooks_dir / "qa.py")],
        input=json.dumps(event),
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout.strip())


class TestQAHookIntegration:
    def test_3_9_empty_checks_passes_trivially(self, tmp_path):
        """3.9: no checks → no block decision."""
        manifest = {"runtime": {"qa": {"checks": []}}}
        event = {"cwd": str(tmp_path), "session_id": "test-session"}
        out = _run_hook_with_manifest(tmp_path, manifest, event)
        assert out.get("decision") != "block"
        assert out.get("checks") == []

    def test_3_8_when_changed_skips_check_when_no_matching_files(self, tmp_path):
        """3.8: check with when_changed: ["*.ts"] skipped when no .ts files touched."""
        manifest = {
            "runtime": {
                "qa": {
                    "checks": [
                        {"name": "ts-build", "command": ["false"], "when_changed": ["*.ts"]},
                    ]
                }
            }
        }
        # session_id: None → _touched defaults to [] (no session = nothing touched)
        # → _files_match([], ["*.ts"]) = False → check is skipped.
        event = {"cwd": str(tmp_path), "session_id": None}
        out = _run_hook_with_manifest(tmp_path, manifest, event)
        assert out.get("decision") != "block"
        checks = {c["name"]: c["status"] for c in out.get("checks", [])}
        assert checks.get("ts-build") == "skip"

    def test_3_7_non_python_check_runs(self, tmp_path):
        """3.7: a non-Python check (true) runs and passes."""
        manifest = {
            "runtime": {
                "qa": {
                    "checks": [
                        ["true"],
                    ]
                }
            }
        }
        event = {"cwd": str(tmp_path), "session_id": None}
        out = _run_hook_with_manifest(tmp_path, manifest, event)
        assert out.get("decision") != "block"
        checks = {c["name"]: c["status"] for c in out.get("checks", [])}
        assert checks.get("true") == "pass"

    def test_failing_check_blocks(self, tmp_path):
        """A check whose command exits nonzero produces a block decision."""
        manifest = {
            "runtime": {
                "qa": {
                    "checks": [
                        {"name": "always-fail", "command": ["false"]},
                    ]
                }
            }
        }
        event = {"cwd": str(tmp_path), "session_id": None}
        out = _run_hook_with_manifest(tmp_path, manifest, event)
        assert out.get("decision") == "block"
        checks = {c["name"]: c["status"] for c in out.get("checks", [])}
        assert checks.get("always-fail") == "fail"

    def test_missing_command_binary_skips(self, tmp_path):
        """A check whose binary doesn't exist is skipped, not failed."""
        manifest = {
            "runtime": {
                "qa": {
                    "checks": [
                        ["__no_such_binary_xyzzy__"],
                    ]
                }
            }
        }
        event = {"cwd": str(tmp_path), "session_id": None}
        out = _run_hook_with_manifest(tmp_path, manifest, event)
        assert out.get("decision") != "block"
        checks = {c["name"]: c["status"] for c in out.get("checks", [])}
        assert checks.get("__no_such_binary_xyzzy__") == "skip"
