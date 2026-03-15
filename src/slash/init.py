"""
slash init — scaffold a slash agent workspace in the current directory.

What it does:
  1. Initialises a git repo at the workspace root if one does not exist yet
  2. Creates the .slash/ control-plane directory tree
  3. Creates the .claude/ SDK config directory with settings.json
  4. Writes CLAUDE.md with agent operating instructions
  5. Creates or updates .gitignore with appropriate entries
  6. Reports what was created and what was skipped (idempotent)

Use `slash update` to refresh hook files after upgrading.
User-editable files (policy manifest only) are never overwritten.
"""
import stat
import subprocess
from pathlib import Path

from rich.console import Console
from rich.table import Table

from slash.templates import (
    AUDIT_HOOK,
    CLAUDE_MD,
    GITIGNORE_BLOCK,
    GITIGNORE_TEMPLATE,
    POLICY_MANIFEST,
    QA_HOOK,
    RESTRICT_WRITES_HOOK,
    SETTINGS_JSON,
)

console = Console()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_git_repo(path: Path) -> bool:
    """Walk up the directory tree looking for a .git directory."""
    current = path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return True
        current = current.parent
    return False


def _git_root(path: Path) -> Path | None:
    """Return the git root if inside a repo, else None."""
    current = path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return None


def _init_git_repo(path: Path) -> str:
    """
    Ensure path is the root of a git repository.

    Runs `git init` when no .git entry exists at path itself.
    Returns "created" if a new repo was initialised, "exists" if one was
    already present.  Raises RuntimeError if git init fails.
    """
    if (path / ".git").exists():
        return "exists"
    result = subprocess.run(
        ["git", "init"],
        cwd=path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return "created"


def _write_file(
    path: Path,
    content: str,
    *,
    executable: bool = False,
    overwrite: bool = False,
) -> str:
    """
    Write content to path, creating parent directories as needed.
    Returns "created", "updated", or "exists" (skipped).

    If overwrite=True and the file exists but content is identical,
    returns "exists" to avoid a spurious "updated" in the status table.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        if not overwrite:
            return "exists"
        if path.read_text() == content:
            return "exists"  # identical — no point rewriting

    existed = path.exists()
    path.write_text(content)

    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return "updated" if existed else "created"


def _touch(path: Path) -> str:
    """Create an empty file (directory marker). Returns status string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return "exists"
    path.touch()
    return "created"


def _update_gitignore(repo_root: Path) -> str:
    """
    Create or update .gitignore with slash-specific ignore patterns.

    - No file          → create a full starter .gitignore from GITIGNORE_TEMPLATE.
    - File exists, .slash/ already excluded → "already configured" (no-op).
    - File exists with old partial block    → upgrade in-place (.slash/audit/ → .slash/).
    - File exists, no slash section         → append GITIGNORE_BLOCK.

    Returns "created", "updated", or "already configured".
    """
    gitignore = repo_root / ".gitignore"
    marker = "# slash —"  # unique marker for the slash section

    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_TEMPLATE)
        return "created"

    existing = gitignore.read_text()

    # Already fully covered — .slash/ (with or without trailing slash) as own line.
    if any(line.strip() in (".slash/", ".slash") for line in existing.splitlines()):
        return "already configured"

    if marker in existing:
        # Old block present but only covers .slash/audit/ — upgrade it.
        updated = existing.replace(".slash/audit/", ".slash/")
        gitignore.write_text(updated)
        return "updated"

    # No slash section at all — append.
    with gitignore.open("a") as f:
        f.write(GITIGNORE_BLOCK)
    return "updated"


# ── Shared scaffolding ────────────────────────────────────────────────────────

# Files managed by slash (safe to overwrite on update)
# Each entry: (path_factory, content, executable)
def _managed_files(target: Path) -> list[tuple[Path, str, bool]]:
    return [
        (target / ".slash" / "hooks" / "restrict_writes.py", RESTRICT_WRITES_HOOK, True),
        (target / ".slash" / "hooks" / "audit.py",           AUDIT_HOOK,           True),
        (target / ".slash" / "hooks" / "qa.py",              QA_HOOK,              True),
        (target / ".claude" / "settings.json",               SETTINGS_JSON,        False),
        (target / "CLAUDE.md",                               CLAUDE_MD,            False),
    ]

# Files owned by the user (never overwritten)
def _user_files(target: Path) -> list[tuple[Path, str, bool]]:
    return [
        (target / ".slash" / "policy" / "manifest.yaml", POLICY_MANIFEST, False),
    ]

# Directory markers
def _markers(target: Path) -> list[Path]:
    return [
        target / ".slash" / "audit" / ".gitkeep",
    ]

def _print_table(results: list[tuple[str, str]]) -> None:
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("File", style="dim")
    table.add_column("Status")

    status_styles = {
        "created":            "[green]created[/green]",
        "updated":            "[yellow]updated[/yellow]",
        "exists":             "[dim]exists — skipped[/dim]",
        "already configured": "[dim]already configured[/dim]",
        "failed":             "[red]failed[/red]",
    }

    for rel, status in results:
        table.add_row(rel, status_styles.get(status, status))

    console.print(table)


# ── init ──────────────────────────────────────────────────────────────────────

def run_init(target: Path) -> None:
    """Strictly additive — creates files that do not exist, skips everything else."""
    target = target.resolve()
    console.print(f"\n[bold]slash init[/bold] — [dim]{target}[/dim]\n")

    results: list[tuple[str, str]] = []

    # Step 1: Ensure the workspace has a git repo at its root.
    try:
        git_status = _init_git_repo(target)
    except RuntimeError as exc:
        console.print(f"[red]✗[/red] git init failed: {exc}")
        git_status = "failed"
    results.append((".git/", git_status))

    git_root = _git_root(target)

    # Step 2: Scaffold slash control-plane files.
    for path, content, executable in _managed_files(target) + _user_files(target):
        rel = path.relative_to(target)
        status = _write_file(path, content, executable=executable, overwrite=False)
        results.append((str(rel), status))

    for path in _markers(target):
        results.append((str(path.relative_to(target)), _touch(path)))

    # Step 3: Configure .gitignore so .slash/ stays out of version control.
    if git_root:
        results.append((".gitignore", _update_gitignore(git_root)))

    _print_table(results)

    console.print("\n[bold]Next steps:[/bold]")
    console.print("  1. Run a task:  [bold]slash run \"<your task description>\"[/bold]")
    console.print("  2. Audit log:   [dim].slash/audit/pipeline.jsonl[/dim]\n")


# ── update ────────────────────────────────────────────────────────────────────

def run_update(target: Path) -> None:
    """Overwrites managed files with latest templates. Never touches user-editable files."""
    target = target.resolve()
    console.print(f"\n[bold]slash update[/bold] — [dim]{target}[/dim]\n")
    console.print("[dim]Refreshing managed hook files from latest templates…[/dim]\n")

    results: list[tuple[str, str]] = []

    # Managed files — always overwrite
    for path, content, executable in _managed_files(target):
        rel = path.relative_to(target)
        status = _write_file(path, content, executable=executable, overwrite=True)
        results.append((str(rel), status))

    # User-editable files — always skip
    for path, content, executable in _user_files(target):
        rel = path.relative_to(target)
        results.append((str(rel), "exists"))

    _print_table(results)
    console.print("[dim]User-editable files (manifest.yaml) were not modified.[/dim]\n")
