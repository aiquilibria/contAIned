"""
Unit tests for contained.cli — pure-logic functions that run without Docker.

Excluded: main (invokes start_repl), init command (invokes run_init + Docker).
"""

from contained.cli import _find_root

# ── _find_root ────────────────────────────────────────────────────────────────


class TestFindRoot:
    def test_returns_cwd_when_no_contained_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _find_root()
        assert result == tmp_path.resolve()

    def test_returns_parent_when_contained_dir_found_above(self, tmp_path, monkeypatch):
        (tmp_path / ".contAIned").mkdir()
        subdir = tmp_path / "src" / "pkg"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        result = _find_root()
        assert result == tmp_path.resolve()

    def test_returns_cwd_itself_when_contained_dir_is_here(self, tmp_path, monkeypatch):
        (tmp_path / ".contAIned").mkdir()
        monkeypatch.chdir(tmp_path)
        result = _find_root()
        assert result == tmp_path.resolve()

    def test_returns_nearest_contained_dir(self, tmp_path, monkeypatch):
        # Both tmp_path and a subdir have .contAIned — nearest (subdir) wins
        (tmp_path / ".contAIned").mkdir()
        subdir = tmp_path / "inner"
        subdir.mkdir()
        (subdir / ".contAIned").mkdir()
        workdir = subdir / "src"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        result = _find_root()
        assert result == subdir.resolve()

    def test_requires_directory_not_file(self, tmp_path, monkeypatch):
        # A .contAIned *file* (not dir) must not match
        (tmp_path / ".contAIned").write_text("not a directory")
        monkeypatch.chdir(tmp_path)
        result = _find_root()
        # .contAIned is not a dir, so falls back to cwd
        assert result == tmp_path.resolve()

    def test_deep_nesting_finds_root(self, tmp_path, monkeypatch):
        (tmp_path / ".contAIned").mkdir()
        deep = tmp_path / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        result = _find_root()
        assert result == tmp_path.resolve()
