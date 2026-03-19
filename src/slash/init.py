"""
slash init — scaffold a slash agent workspace in the current directory.

What it does:
  1. Builds the slash Docker image and creates required network/volume
  2. Initialises a git repo at the workspace root if one does not exist yet
  3. Creates the .slash/ control-plane directory tree
  4. Creates the .claude/ SDK config directory with settings.json
  5. Writes CLAUDE.md with agent operating instructions
  6. Creates or updates .gitignore with appropriate entries
  7. Reports what was created and what was skipped (idempotent)

Docker is the only supported runtime.  The agent always runs inside the
slash container; SLASH_FORCE_LOCAL=1 is reserved for the in-container process.

Use `slash update` to refresh hook files after upgrading.
User-editable files (policy manifest only) are never overwritten.

Manifest location: .slash/manifest.yaml  (new)
  Legacy path     : .slash/policy/manifest.yaml  (supported via compat shim)
"""
import os
import stat
import subprocess
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from slash.templates import (
    AUDIT_HOOK,
    CLAUDE_MD,
    GITIGNORE_BLOCK,
    GITIGNORE_TEMPLATE,
    POLICY_LOADER_HOOK,
    QA_HOOK,
    RESTRICT_BASH_HOOK,
    RESTRICT_READS_HOOK,
    RESTRICT_WRITES_HOOK,
    SETTINGS_JSON,
    STATUSLINE_PY,
    SUBAGENT_START_HOOK,
    SUBAGENT_STOP_HOOK,
    SUMMARIZER_HOOK,
    TRACER_POST_HOOK,
    TRACER_PRE_HOOK,
    USER_PROMPT_SUBMIT_HOOK,
)

console = Console()

_DEFAULT_DOCKER_CONFIG: dict = {
    "image": "slash:latest",
    "memory": "2g",
    "cpus": 2,
    "network": "slash-net",
    "agent_config_volume": "slash-agent-config",
}


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
    settings = SETTINGS_JSON.format(workspace=str(target.resolve()))
    return [
        (target / ".slash" / "hooks" / "_policy.py",          POLICY_LOADER_HOOK,   False),
        (target / ".slash" / "hooks" / "restrict_reads.py",   RESTRICT_READS_HOOK,  True),
        (target / ".slash" / "hooks" / "restrict_writes.py",  RESTRICT_WRITES_HOOK, True),
        (target / ".slash" / "hooks" / "restrict_bash.py",    RESTRICT_BASH_HOOK,   True),
        (target / ".slash" / "hooks" / "audit.py",            AUDIT_HOOK,           True),
        (target / ".slash" / "hooks" / "tracer_pre.py",       TRACER_PRE_HOOK,      True),
        (target / ".slash" / "hooks" / "tracer_post.py",      TRACER_POST_HOOK,     True),
        (target / ".slash" / "hooks" / "subagent_start.py",   SUBAGENT_START_HOOK,  True),
        (target / ".slash" / "hooks" / "subagent_stop.py",    SUBAGENT_STOP_HOOK,   True),
        (target / ".slash" / "hooks" / "summarizer.py",       SUMMARIZER_HOOK,      True),
        (target / ".slash" / "hooks" / "qa.py",               QA_HOOK,              True),
        (target / ".slash" / "hooks" / "user_prompt_submit.py", USER_PROMPT_SUBMIT_HOOK, True),
        (target / ".claude" / "settings.json",                settings,             False),
        (target / ".claude" / "statusline.py",               STATUSLINE_PY,        True),
        (target / "CLAUDE.md",                                CLAUDE_MD,            False),
    ]


def _sync_manifest(path: Path, template_content: str) -> str:
    """Ensure manifest.yaml contains every key defined in the template.

    Merges template defaults into the existing file: keys present in the
    template but absent in the file are added; values the user has already
    set are never overwritten.

    Returns "created", "updated", or "exists".
    """
    import yaml  # pyyaml — project dependency

    if not path.exists():
        return _write_file(path, template_content, overwrite=False)

    try:
        template_data: dict = yaml.safe_load(template_content) or {}
        existing_text: str = path.read_text()
        existing_data: dict = yaml.safe_load(existing_text) or {}
    except Exception:
        return "exists"  # unparseable — leave untouched

    def _shape_merge(template: dict, existing: dict) -> dict:
        """Return a dict shaped exactly like *template*, with values from *existing*.

        - Keys present in *template* but absent in *existing* → template default.
        - Keys present in *existing* but absent in *template* → dropped (old format).
        - Shared dict-valued keys → recurse.
        """
        result = {}
        for key, tmpl_val in template.items():
            if key in existing:
                exist_val = existing[key]
                if isinstance(tmpl_val, dict) and isinstance(exist_val, dict):
                    result[key] = _shape_merge(tmpl_val, exist_val)
                else:
                    result[key] = exist_val
            else:
                result[key] = tmpl_val
        return result

    merged = _shape_merge(template_data, existing_data)

    if merged == existing_data:
        return "exists"

    path.write_text(
        yaml.dump(merged, default_flow_style=False, sort_keys=False, allow_unicode=True)
    )
    return "updated"


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
        "migrated":           "[green]migrated[/green]",
        "updated":            "[yellow]updated[/yellow]",
        "exists":             "[dim]exists — skipped[/dim]",
        "already configured": "[dim]already configured[/dim]",
        "failed":             "[red]failed[/red]",
    }

    for rel, status in results:
        table.add_row(rel, status_styles.get(status, status))

    console.print(table)


# ── Docker setup ──────────────────────────────────────────────────────────────

def _slash_version() -> str:
    """Return the installed slash package version, or 'unknown' if undetectable."""
    try:
        import importlib.metadata
        return importlib.metadata.version("slash")
    except Exception:
        return "unknown"


def _docker_setup(config: dict, workspace: Path, *, rebuild: bool = False) -> None:
    """
    Perform Docker infrastructure setup for a workspace:
      1. Build (or rebuild) the ``slash:latest`` image from the bundled Dockerfile.
         The image is stamped with a ``slash.version`` label.  If the image already
         exists *and* its label matches the currently installed slash version, the
         build is skipped.  A version mismatch (e.g. after ``pip install --upgrade
         slash``) triggers an automatic rebuild so the container always runs the
         same slash code as the host.
         Pass ``rebuild=True`` to force a full rebuild regardless of the version label
         (equivalent to ``docker build --no-cache`` in intent, though the layer cache
         is still used to keep the build fast).
      2. Create the ``slash-agent-config`` named volume.
      3. Create the ``slash-net`` bridge network.

    Raises ``RuntimeError`` on any failure (including Docker not found).
    """
    from slash.docker_runner import _find_docker

    # Locate docker executable (raises FileNotFoundError with helpful message)
    try:
        docker_bin = _find_docker()
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc

    import slash  # used to locate the bundled Dockerfile

    slash_pkg = Path(slash.__file__).parent
    dockerfile = slash_pkg / "runtime" / "Dockerfile"
    # The build context must be the project root (where pyproject.toml lives)
    project_root = slash_pkg.parent.parent

    # 1. Build image — skip only when image exists AND its version label matches
    image = config["image"]
    current_version = _slash_version()

    needs_build = True
    if rebuild:
        console.print(
            f"  Image [bold]{image}[/bold] — forced rebuild requested."
        )
    else:
        inspect = subprocess.run(
            [docker_bin, "image", "inspect",
             "--format", "{{index .Config.Labels \"slash.version\"}}",
             image],
            capture_output=True,
            text=True,
        )
        if inspect.returncode == 0:
            image_version = inspect.stdout.strip()
            if image_version == current_version:
                console.print(
                    f"  Image [bold]{image}[/bold] is up to date "
                    f"([dim]{current_version}[/dim]) — skipping build."
                )
                needs_build = False
            else:
                label_display = image_version if image_version else "unlabelled"
                console.print(
                    f"  Image [bold]{image}[/bold] is stale "
                    f"([dim]{label_display}[/dim] → [dim]{current_version}[/dim]) — rebuilding."
                )

    if needs_build:
        console.print(f"  Building image [bold]{image}[/bold] …", end="")
        result = subprocess.run(
            [
                docker_bin, "build",
                "--build-arg", f"HOST_UID={os.getuid()}",
                "--build-arg", f"HOST_GID={os.getgid()}",
                "--label", f"slash.version={current_version}",
                "-t", image,
                "-f", str(dockerfile),
                str(project_root),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(" [red]failed[/red]")
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        console.print(" [green]done[/green]")

    # 2. Create named volume
    vol = config["agent_config_volume"]
    result = subprocess.run(
        [docker_bin, "volume", "create", vol],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker volume create {vol}: {result.stderr.strip()}")
    console.print(f"  Volume [bold]{vol}[/bold] ready.")

    # 3. Create network (idempotent — "already exists" is not an error)
    net = config["network"]
    result = subprocess.run(
        [docker_bin, "network", "create", "--driver", "bridge", net],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "already exists" not in result.stderr:
        raise RuntimeError(f"docker network create {net}: {result.stderr.strip()}")
    console.print(f"  Network [bold]{net}[/bold] ready.")


# ── Manifest builder ──────────────────────────────────────────────────────────

def _build_manifest(
    docker_config: dict | None,
    model: str,
    qa_choices: dict | None = None,
) -> str:
    """Return a YAML string for the complete manifest based on wizard choices."""
    import yaml

    default_qa = {
        "syntax": True,
        "lint":   True,
        "format": True,
        "type":   True,
    }
    qa = {**default_qa, **(qa_choices or {})}

    manifest: dict = {
        "runtime": {},
        "policy": {
            "secrets": {
                "reads":         "block",
                "writes":        "block",
                "bash_reads":    "block",
                "safe_variants": "allow",
            },
            "bash": {
                "destructive":          "block",
                "privilege_escalation": "block",
                "network_exfiltration": "block",
                "git_mutations":        "escalate",
                "package_publish":      "block",
            },
            "audit": {"enabled": True},
            "qa": qa,
        },
        "agent": {
            "model": model,
        },
    }

    if docker_config:
        manifest["runtime"]["docker"] = {
            "image":               docker_config["image"],
            "memory":              docker_config["memory"],
            "cpus":                docker_config["cpus"],
            "network":             docker_config["network"],
            "agent_config_volume": docker_config["agent_config_volume"],
        }

    return yaml.dump(manifest, default_flow_style=False, sort_keys=False, allow_unicode=True)


# ── init ──────────────────────────────────────────────────────────────────────

def run_init(target: Path, *, force: bool = False, rebuild: bool = False) -> None:
    """
    Interactive first-time workspace setup.

    Runs the Phase 1 / 2 / 3 wizard on first initialisation.  If the manifest
    already exists the wizard is skipped and managed files are refreshed —
    unless ``force=True``, which re-runs the full wizard (useful for switching
    from local to docker mode or vice versa).

    Pass ``rebuild=True`` (``--rebuild`` on the CLI) to force a Docker image
    rebuild even when the image's version label already matches the installed
    slash version.
    """
    target = target.resolve()
    console.print(f"\n[bold]slash init[/bold] — [dim]{target}[/dim]\n")

    manifest_new = target / ".slash" / "manifest.yaml"
    manifest_old = target / ".slash" / "policy" / "manifest.yaml"
    already_init = manifest_new.exists() or manifest_old.exists()

    # Docker is the only supported runtime.
    docker_config = dict(_DEFAULT_DOCKER_CONFIG)

    if already_init and not force:
        console.print("[dim]Workspace already initialised — refreshing managed files.[/dim]\n")
        # Preserve existing manifest; just refresh hooks and check docker image.
        manifest_content: str | None = None
        try:
            _docker_setup(docker_config, target, rebuild=rebuild)
        except RuntimeError as exc:
            console.print(f"\n[yellow]Warning:[/yellow] Docker image check failed: {exc}\n")
    else:
        if already_init and force:
            console.print("[yellow]Re-running setup wizard (--force). Your current configuration will be replaced.[/yellow]\n")
        console.print("Welcome to slash.\n")
        console.print(
            f"  Docker configuration:\n"
            f"    image:   [bold]{docker_config['image']}[/bold]"
            f"  (built from slash runtime Dockerfile)\n"
            f"    memory:  {docker_config['memory']}  |  "
            f"cpus: {docker_config['cpus']}  |  "
            f"network: {docker_config['network']}\n"
        )
        try:
            _docker_setup(docker_config, target, rebuild=rebuild)
        except RuntimeError as exc:
            console.print(f"\n[red]✗[/red] Docker setup failed: {exc}")
            raise SystemExit(1)

        console.print()
        console.print("  [dim]Audit logging:      always on[/dim]")
        console.print("  [dim]git push / --force: requires escalation[/dim]")
        console.print("  [dim].slash/ protection: always enforced[/dim]")
        console.print()

        # ── QA checks ────────────────────────────────────────────────────────
        console.print("? QA checks (enabled by default — press Enter to keep):")
        qa_syntax  = click.confirm("    syntax  (py_compile)",          default=True)
        qa_lint    = click.confirm("    lint    (ruff check)",           default=True)
        qa_format  = click.confirm("    format  (ruff format --check)",  default=True)
        qa_type    = click.confirm("    type    (pyright)",              default=True)
        console.print()

        qa_choices = {
            "syntax": qa_syntax,
            "lint":   qa_lint,
            "format": qa_format,
            "type":   qa_type,
        }

        # ── Model ─────────────────────────────────────────────────────────────
        model = click.prompt("? Default model", default="claude-sonnet-4-6")
        console.print()

        manifest_content = _build_manifest(
            docker_config=docker_config,
            model=model,
            qa_choices=qa_choices,
        )

    results: list[tuple[str, str]] = []

    # ── Git repo ──────────────────────────────────────────────────────────────
    try:
        git_status = _init_git_repo(target)
    except RuntimeError as exc:
        console.print(f"[red]✗[/red] git init failed: {exc}")
        git_status = "failed"
    results.append((".git/", git_status))

    git_root = _git_root(target)

    # ── Managed files (hooks, settings, CLAUDE.md) ────────────────────────────
    # Overwrite on re-runs so that slash init refreshes hooks to the latest
    # bundled templates.  On first-time init the files don't exist yet, so
    # overwrite=False and overwrite=True are equivalent.
    for path, content, executable in _managed_files(target):
        rel = path.relative_to(target)
        status = _write_file(path, content, executable=executable, overwrite=already_init)
        results.append((str(rel), status))

    # ── Manifest ──────────────────────────────────────────────────────────────
    if manifest_content is not None:
        # First-time init or --force: write wizard-generated manifest
        status = _write_file(manifest_new, manifest_content, overwrite=force)
        results.append((".slash/manifest.yaml", status))
    elif manifest_old.exists() and not manifest_new.exists():
        # Migrate old path → new path, preserving content
        manifest_new.parent.mkdir(parents=True, exist_ok=True)
        manifest_new.write_text(manifest_old.read_text())
        results.append((".slash/manifest.yaml", "migrated"))
    else:
        # Sync any new template keys introduced in this slash version,
        # preserving all user-configured values.
        from slash.templates import POLICY_MANIFEST
        results.append((".slash/manifest.yaml", _sync_manifest(manifest_new, POLICY_MANIFEST)))

    # ── Directory markers ─────────────────────────────────────────────────────
    for path in _markers(target):
        results.append((str(path.relative_to(target)), _touch(path)))

    # ── .gitignore ────────────────────────────────────────────────────────────
    if git_root:
        results.append((".gitignore", _update_gitignore(git_root)))

    _print_table(results)

    console.print(f"\n[bold]Workspace initialised.[/bold] [dim]runtime: docker ({docker_config['image']})[/dim]")

    console.print("  slash run \"<your task here>\"  # For one-time tasks")
    console.print("  slash  # For REPL\n")


