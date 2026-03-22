"""
contAIned init — scaffold a contAIned agent workspace in the current directory.

What it does:
  1. Builds the contAIned Docker image and creates required network/volume
  2. Initialises a git repo at the workspace root if one does not exist yet
  3. Creates the .contAIned/ control-plane directory tree
  4. Creates the .claude/ directory with statusline.py
  5. Writes CLAUDE.md with agent operating instructions
  6. Creates or updates .gitignore with appropriate entries
  7. Reports what was created and what was skipped (idempotent)

Docker is the only supported runtime.  The agent always runs inside the
contAIned container; contAIned_FORCE_LOCAL=1 is reserved for the in-container process.

Use `contAIned update` to refresh hook files after upgrading.
User-editable files (policy manifest only) are never overwritten.

Manifest location: .contAIned/manifest.yaml  (new)
  Legacy path     : .contAIned/policy/manifest.yaml  (supported via compat shim)
"""

import os
import stat
import subprocess
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from contained.templates import (
    AUDIT_HOOK,
    CLAUDE_MD,
    GITIGNORE_BLOCK,
    GITIGNORE_TEMPLATE,
    PERMISSION_REQUEST_HOOK,
    POLICY_LOADER_HOOK,
    QA_HOOK,
    RESTRICT_BASH_HOOK,
    RESTRICT_READS_HOOK,
    RESTRICT_WRITES_HOOK,
    SUBAGENT_START_HOOK,
    SUBAGENT_STOP_HOOK,
    SUMMARIZER_HOOK,
    TRACER_POST_HOOK,
    TRACER_PRE_HOOK,
    USER_PROMPT_SUBMIT_HOOK,
)

console = Console()

_DEFAULT_DOCKER_CONFIG: dict = {
    "image": "contained:latest",
    "memory": "2g",
    "cpus": 2,
    "network": "contAIned-net",
    "agent_config_volume": "contAIned-agent-config",
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
    Create or update .gitignore with contAIned-specific ignore patterns.

    - No file          → create a full starter .gitignore from GITIGNORE_TEMPLATE.
    - File exists, .contAIned/ already excluded → "already configured" (no-op).
    - File exists with old partial block    → upgrade in-place
      (.contAIned/audit/ → .contAIned/).
    - File exists, no contAIned section         → append GITIGNORE_BLOCK.

    Returns "created", "updated", or "already configured".
    """
    gitignore = repo_root / ".gitignore"
    marker = "# contAIned —"  # unique marker for the contAIned section

    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_TEMPLATE)
        return "created"

    existing = gitignore.read_text()

    # Already fully covered — .contAIned/ (with or without trailing slash) as own line.
    if any(line.strip() in (".contAIned/", ".contAIned") for line in existing.splitlines()):
        return "already configured"

    if marker in existing:
        # Old block present but only covers .contAIned/audit/ — upgrade it.
        updated = existing.replace(".contAIned/audit/", ".contAIned/")
        gitignore.write_text(updated)
        return "updated"

    # No contAIned section at all — append.
    with gitignore.open("a") as f:
        f.write(GITIGNORE_BLOCK)
    return "updated"


# ── settings.json migration ───────────────────────────────────────────────────


def _migrate_settings_json(target: Path) -> str:
    """
    Retire .claude/settings.json — all its content is owned by managed-settings.json.

    managed-settings.json is baked into the Docker image and covers hook
    registration, sandbox rules, permissions, statusLine, and attribution.
    Claude Code merges hooks from all settings levels, so any "hooks" key in
    .claude/settings.json causes every hook to fire twice.  The remaining keys
    (permissions, sandbox, statusLine) are redundant with managed-settings and
    serve no purpose since Claude Code always runs inside the container.

    If .claude/settings.json (or .claude/settings.local.json) exists:
      - Rename it to .claude/settings.json.bak.<timestamp> (non-destructive)
      - Write nothing in its place
      - Return "migrated"

    If neither file exists: no-op.
    """
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    migrated = False

    for filename in ("settings.json", "settings.local.json"):
        settings_path = target / ".claude" / filename
        if settings_path.exists():
            backup = settings_path.with_name(f"{filename}.bak.{timestamp}")
            settings_path.rename(backup)
            migrated = True

    return "migrated" if migrated else "exists"


# ── Shared scaffolding ────────────────────────────────────────────────────────


# Files managed by contAIned (safe to overwrite on update)
# Each entry: (path_factory, content, executable)
def _managed_files(target: Path) -> list[tuple[Path, str, bool]]:
    return [
        (target / ".contAIned" / "hooks" / "_policy.py", POLICY_LOADER_HOOK, False),
        (
            target / ".contAIned" / "hooks" / "restrict_reads.py",
            RESTRICT_READS_HOOK,
            True,
        ),
        (
            target / ".contAIned" / "hooks" / "restrict_writes.py",
            RESTRICT_WRITES_HOOK,
            True,
        ),
        (
            target / ".contAIned" / "hooks" / "restrict_bash.py",
            RESTRICT_BASH_HOOK,
            True,
        ),
        (target / ".contAIned" / "hooks" / "audit.py", AUDIT_HOOK, True),
        (
            target / ".contAIned" / "hooks" / "permission_request.py",
            PERMISSION_REQUEST_HOOK,
            True,
        ),
        (target / ".contAIned" / "hooks" / "tracer_pre.py", TRACER_PRE_HOOK, True),
        (target / ".contAIned" / "hooks" / "tracer_post.py", TRACER_POST_HOOK, True),
        (
            target / ".contAIned" / "hooks" / "subagent_start.py",
            SUBAGENT_START_HOOK,
            True,
        ),
        (
            target / ".contAIned" / "hooks" / "subagent_stop.py",
            SUBAGENT_STOP_HOOK,
            True,
        ),
        (target / ".contAIned" / "hooks" / "summarizer.py", SUMMARIZER_HOOK, True),
        (target / ".contAIned" / "hooks" / "qa.py", QA_HOOK, True),
        (
            target / ".contAIned" / "hooks" / "user_prompt_submit.py",
            USER_PROMPT_SUBMIT_HOOK,
            True,
        ),
        (target / "CLAUDE.md", CLAUDE_MD, False),
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
        target / ".contAIned" / "audit" / ".gitkeep",
    ]


def _print_table(results: list[tuple[str, str]]) -> None:
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("File", style="dim")
    table.add_column("Status")

    status_styles = {
        "created": "[green]created[/green]",
        "migrated": "[green]migrated[/green]",
        "updated": "[yellow]updated[/yellow]",
        "exists": "[dim]exists — skipped[/dim]",
        "already configured": "[dim]already configured[/dim]",
        "failed": "[red]failed[/red]",
        "skipped (Sigstore disabled)": "[dim]skipped (Sigstore disabled)[/dim]",
    }

    for rel, status in results:
        table.add_row(rel, status_styles.get(status, status))

    console.print(table)


# ── Docker setup ──────────────────────────────────────────────────────────────


def _contAIned_version() -> str:
    """Return the installed contAIned package version, or 'unknown' if undetectable."""
    try:
        import importlib.metadata

        return importlib.metadata.version("contAIned")
    except Exception:
        return "unknown"


def _docker_setup(
    config: dict,
    workspace: Path,
    *,
    rebuild: bool = False,
    manifest_content: str | None = None,
    managed_settings_content: str | None = None,
) -> bool:
    """
    Perform Docker infrastructure setup for a workspace:
      1. Build (or rebuild) the ``contained:latest`` image from the bundled Dockerfile.
         The image is stamped with a ``contAIned.version`` label.  If the image already
         exists *and* its label matches the currently installed contAIned version, the
         build is skipped.  A version mismatch (e.g. after ``pip install --upgrade
         contAIned``) triggers an automatic rebuild so the container always runs the
         same contAIned code as the host.
         Pass ``rebuild=True`` to force a full rebuild regardless of the version label
         (equivalent to ``docker build --no-cache`` in intent, though the layer cache
         is still used to keep the build fast).
         Pass ``manifest_content`` (raw YAML string) to bake the operator manifest into
         the image as ``/etc/contained/manifest.yaml``.  When present, hooks read policy
         parameters from the image rather than the workspace.
         Pass ``managed_settings_content`` (JSON string) to bake a generated
         managed-settings.json into the image as
         ``/etc/claude-code/managed-settings.json``, replacing the static template.
      2. Create the ``contAIned-agent-config`` named volume.
      3. Create the ``contAIned-net`` bridge network.

    Returns ``True`` if the image was (re)built, ``False`` if it was up to date
    and the build was skipped.  Callers use this to decide whether to re-sign.

    Raises ``RuntimeError`` on any failure (including Docker not found).
    """
    from contained.docker_runner import _find_docker

    # Locate docker executable (raises FileNotFoundError with helpful message)
    try:
        docker_bin = _find_docker()
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc

    import contained  # used to locate the bundled Dockerfile

    contAIned_pkg = Path(contained.__file__).parent
    dockerfile = contAIned_pkg / "runtime" / "Dockerfile"
    # The build context must be the project root (where pyproject.toml lives)
    project_root = contAIned_pkg.parent.parent

    # 1. Build image — skip only when image exists, version label matches, and manifest hash matches
    import base64
    import hashlib

    manifest_hash = hashlib.sha256((manifest_content or "").encode()).hexdigest()[:16]
    manifest_b64 = base64.b64encode((manifest_content or "").encode()).decode()
    managed_settings_b64 = base64.b64encode((managed_settings_content or "").encode()).decode()

    image = config["image"]
    current_version = _contAIned_version()

    needs_build = True
    if rebuild:
        console.print(f"  Image [bold]{image}[/bold] — forced rebuild requested.")
    else:
        _label_fmt = (
            '{{index .Config.Labels "contAIned.version"}}'
            '|{{index .Config.Labels "contAIned.manifest_hash"}}'
        )
        inspect = subprocess.run(
            [docker_bin, "image", "inspect", "--format", _label_fmt, image],
            capture_output=True,
            text=True,
        )
        if inspect.returncode == 0:
            parts = inspect.stdout.strip().split("|", 1)
            image_version = parts[0]
            image_manifest_hash = parts[1] if len(parts) > 1 else ""
            if image_version == current_version and image_manifest_hash == manifest_hash:
                console.print(
                    f"  Image [bold]{image}[/bold] is up to date "
                    f"([dim]{current_version}[/dim]) — skipping build."
                )
                needs_build = False
            else:
                if image_version != current_version:
                    label_display = image_version if image_version else "unlabelled"
                    console.print(
                        f"  Image [bold]{image}[/bold] is stale "
                        f"([dim]{label_display}[/dim] → [dim]{current_version}[/dim])"
                        " — rebuilding."
                    )
                else:
                    console.print(f"  Image [bold]{image}[/bold] policy has changed — rebuilding.")

    if needs_build:
        # Warn if a session is currently running — provenance.yaml will be
        # refreshed mid-session, making it inconsistent with the running image.
        ps = subprocess.run(
            [docker_bin, "ps", "--filter", f"name=contAIned-{workspace.name}-", "--quiet"],
            capture_output=True,
            text=True,
        )
        if ps.stdout.strip():
            console.print(
                f"  [yellow]Warning:[/yellow] a contAIned session for "
                f"[bold]{workspace.name}[/bold] appears to be running. "
                "Rebuilding will update provenance.yaml mid-session."
            )
        console.print(f"  Building image [bold]{image}[/bold] …", end="")
        build_cmd = [
            docker_bin,
            "build",
            "--build-arg",
            f"HOST_UID={os.getuid()}",
            "--build-arg",
            f"HOST_GID={os.getgid()}",
            "--label",
            f"contAIned.version={current_version}",
            "--label",
            f"contAIned.manifest_hash={manifest_hash}",
            "--build-arg",
            f"MANIFEST_CONTENT={manifest_b64}",
            "--build-arg",
            f"MANAGED_SETTINGS_CONTENT={managed_settings_b64}",
        ]
        build_cmd += ["-t", image, "-f", str(dockerfile), str(project_root)]
        result = subprocess.run(
            build_cmd,
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

    return needs_build


# ── Managed-settings builder ──────────────────────────────────────────────────


def _build_managed_settings(manifest: dict) -> str:
    """Generate managed-settings.json content from the parsed manifest dict.

    The returned JSON is baked into the Docker image as
    /etc/claude-code/managed-settings.json, replacing the static template.
    Dynamic sections (WebFetch allow rules, sandbox.network.allowedDomains, MCP
    server rules, skill allow rules) are derived from policy.network,
    policy.mcp, and policy.skills in the manifest.
    """
    import json

    network = manifest.get("policy", {}).get("network", {})
    allowed_domains: list[str] = network.get(
        "allowed_domains",
        ["api.anthropic.com", "code.claude.com", "docs.anthropic.com"],
    )
    mcp_servers: list[str] = manifest.get("policy", {}).get("mcp", {}).get("approved_servers", [])
    approved_skills: list[str] = (
        manifest.get("policy", {}).get("skills", {}).get("approved_skills", [])
    )

    # Permission allow rules: workspace access + built-in plugin + dynamic domain/MCP/skill rules
    allow_rules: list[str] = [
        "Read(/workspace/**)",
        "Glob(/workspace/**)",
        "Grep(/workspace/**)",
        "mcp__plugin_contained_tracer__*",  # contAIned built-in tracer plugin (always present)
        "Skill(contained:tracer)",  # contAIned built-in tracer skill (always present)
    ]
    for domain in allowed_domains:
        allow_rules.append(f"WebFetch(domain:{domain})")
    for server in mcp_servers:
        allow_rules.append(f"mcp__{server}__*")
    for skill in approved_skills:
        allow_rules.append(f"Skill({skill})")

    hook_cmd = "python3 /workspace/.contAIned/hooks/{}.py"
    settings: dict = {
        "permissions": {
            "allow": allow_rules,
            "ask": ["WebFetch", "WebSearch"],
            "disableBypassPermissionsMode": "disable",
            "allowManagedPermissionRulesOnly": True,
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read|Glob|Grep",
                    "hooks": [{"type": "command", "command": hook_cmd.format("restrict_reads")}],
                },
                {
                    "matcher": "Write|Edit|MultiEdit",
                    "hooks": [
                        {"type": "command", "command": hook_cmd.format("restrict_writes")},
                        {"type": "command", "command": hook_cmd.format("tracer_pre")},
                    ],
                },
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": hook_cmd.format("restrict_bash")}],
                },
            ],
            "PostToolUse": [
                {
                    "matcher": "Write|Edit|MultiEdit",
                    "hooks": [{"type": "command", "command": hook_cmd.format("tracer_post")}],
                },
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": hook_cmd.format("audit")}],
                },
            ],
            "SubagentStart": [
                {"hooks": [{"type": "command", "command": hook_cmd.format("subagent_start")}]},
            ],
            "SubagentStop": [
                {"hooks": [{"type": "command", "command": hook_cmd.format("subagent_stop")}]},
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": hook_cmd.format("summarizer")}]},
            ],
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": hook_cmd.format("user_prompt_submit")}]},
            ],
            "PermissionRequest": [
                {"hooks": [{"type": "command", "command": hook_cmd.format("permission_request")}]},
            ],
        },
        "sandbox": {
            "enabled": True,
            "enableWeakerNestedSandbox": True,
            "allowUnsandboxedCommands": False,
            "network": {
                "allowedDomains": allowed_domains,
                "allowManagedDomainsOnly": True,
            },
            "filesystem": {
                "denyWrite": [".contAIned", ".claude/settings.json"],
            },
        },
        "allowManagedHooksOnly": True,
        "statusLine": {
            "type": "command",
            "command": "python3 /etc/contained/statusline.py",
        },
        "attribution": {
            "commit": "Generated with Claude Code on cont[AI]ned",
            "pr": "Generated with Claude Code on cont[AI]ned",
        },
    }

    return json.dumps(settings, indent=2)


# ── Policy wizard ─────────────────────────────────────────────────────────────


def _run_wizard(docker_config: dict) -> str:
    """Run the interactive policy wizard and return manifest YAML."""
    console.print("  Docker configuration:")
    console.print(
        f"    image:   [bold]{docker_config['image']}[/bold]"
        f"  (built from contained runtime Dockerfile)"
    )
    console.print(
        f"    memory:  {docker_config['memory']}  |  "
        f"cpus: {docker_config['cpus']}  |  "
        f"network: {docker_config['network']}\n"
    )
    console.print("  [dim]Audit logging:      always on[/dim]")
    console.print("  [dim]git push / --force: requires escalation[/dim]")
    console.print("  [dim].contAIned/ protection: always enforced[/dim]")
    console.print()

    # ── QA checks ────────────────────────────────────────────────────────────
    console.print("? QA checks (enabled by default — press Enter to keep):")
    qa_syntax = click.confirm("    syntax  (py_compile)", default=True)
    qa_lint = click.confirm("    lint    (ruff check)", default=True)
    qa_format = click.confirm("    format  (ruff format --check)", default=True)
    qa_type = click.confirm("    type    (pyright)", default=True)
    qa_test = click.confirm("    test    (pytest tests/)", default=True)
    qa_coverage = click.confirm("    coverage (pytest --cov)", default=True)
    coverage_threshold = 80
    if qa_coverage:
        coverage_threshold = click.prompt("    coverage threshold (%)", default=80, type=int)
    console.print()

    qa_choices = {
        "syntax": qa_syntax,
        "lint": qa_lint,
        "format": qa_format,
        "type": qa_type,
        "test": qa_test,
        "coverage": qa_coverage,
        "coverage_threshold": coverage_threshold,
    }

    # ── Model ─────────────────────────────────────────────────────────────────
    model = click.prompt("? Default model", default="claude-sonnet-4-6")
    console.print()

    # ── Network ───────────────────────────────────────────────────────────────
    network_enabled = click.confirm(
        "? Enable network domain policy"
        " (api.anthropic.com, code.claude.com, docs.anthropic.com always allowed)",
        default=True,
    )
    network_extra_domains: list[str] = []
    if network_enabled:
        raw = click.prompt(
            "  Additional allowed domains (comma-separated, or Enter to skip)",
            default="",
        )
        network_extra_domains = [d.strip() for d in raw.split(",") if d.strip()]
    console.print()

    # ── MCP servers ───────────────────────────────────────────────────────────
    raw = click.prompt(
        "? Approved MCP servers (comma-separated, or Enter to skip)",
        default="",
    )
    mcp_approved_servers = [s.strip() for s in raw.split(",") if s.strip()]
    console.print()

    # ── Skills ────────────────────────────────────────────────────────────────
    raw = click.prompt(
        "? Approved skills (comma-separated, or Enter to skip)",
        default="",
    )
    approved_skills = [s.strip() for s in raw.split(",") if s.strip()]
    console.print()

    # ── Sigstore ──────────────────────────────────────────────────────────────
    console.print()
    sigstore_enabled = click.confirm(
        "? Enable build provenance (Sigstore / cosign required)",
        default=True,
    )
    if sigstore_enabled:
        from contained.docker_runner import _find_cosign

        try:
            _find_cosign()
        except FileNotFoundError as exc:
            console.print(f"\n[red]✗[/red] {exc}")
            raise SystemExit(1)
    console.print()

    return _build_manifest(
        docker_config=docker_config,
        model=model,
        qa_choices=qa_choices,
        network_enabled=network_enabled,
        network_extra_domains=network_extra_domains,
        mcp_approved_servers=mcp_approved_servers,
        approved_skills=approved_skills,
        sigstore_enabled=sigstore_enabled,
    )


# ── Manifest builder ──────────────────────────────────────────────────────────


def _build_manifest(
    docker_config: dict | None,
    model: str,
    qa_choices: dict | None = None,
    network_enabled: bool = True,
    network_extra_domains: list[str] | None = None,
    mcp_approved_servers: list[str] | None = None,
    approved_skills: list[str] | None = None,
    sigstore_enabled: bool = True,
) -> str:
    """Return a YAML string for the complete manifest based on wizard choices."""
    import yaml

    default_qa = {
        "syntax": True,
        "lint": True,
        "format": True,
        "type": True,
        "test": True,
        "coverage": True,
        "coverage_threshold": 80,
    }
    qa = {**default_qa, **(qa_choices or {})}

    manifest: dict = {
        "runtime": {},
        "policy": {
            "secrets": {
                "reads": "block",
                "writes": "block",
                "bash_reads": "block",
                "safe_variants": "allow",
            },
            "bash": {
                "destructive": "block",
                "privilege_escalation": "block",
                "network_exfiltration": "block",
                "git_mutations": "escalate",
                "package_publish": "block",
            },
            "audit": {"enabled": True},
            "network": {
                "enabled": network_enabled,
                "allowed_domains": ["api.anthropic.com", "code.claude.com", "docs.anthropic.com"]
                + (network_extra_domains or []),
            },
            "mcp": {
                "approved_servers": mcp_approved_servers or [],
            },
            "skills": {
                "approved_skills": approved_skills or [],
            },
            "qa": qa,
        },
        "agent": {
            "model": model,
        },
    }

    sigstore: dict = {"enabled": sigstore_enabled}
    if sigstore_enabled:
        sigstore["rekor_url"] = "https://rekor.sigstore.dev"
        sigstore["fulcio_url"] = "https://fulcio.sigstore.dev"
    manifest["sigstore"] = sigstore

    if docker_config:
        manifest["runtime"]["docker"] = {
            "image": docker_config["image"],
            "memory": docker_config["memory"],
            "cpus": docker_config["cpus"],
            "network": docker_config["network"],
            "agent_config_volume": docker_config["agent_config_volume"],
        }

    return yaml.dump(manifest, default_flow_style=False, sort_keys=False, allow_unicode=True)


# ── Provenance ────────────────────────────────────────────────────────────────


def _write_provenance(target: Path, data: dict) -> str:
    """Write .contAIned/provenance.yaml and return a status string."""
    import yaml

    provenance = {
        "schema_version": 1,
        "image_digest": data["image_digest"],
        "rekor_log_index": data["rekor_log_index"],
        "rekor_entry_url": data["rekor_entry_url"],
        "operator_identity": data["operator_identity"],
        "oidc_issuer": data["oidc_issuer"],
        "signed_at": data["signed_at"],
    }
    path = target / ".contAIned" / "provenance.yaml"
    return _write_file(
        path,
        yaml.dump(provenance, default_flow_style=False, sort_keys=False, allow_unicode=True),
        overwrite=True,
    )


# ── init ──────────────────────────────────────────────────────────────────────


def run_init(
    target: Path,
    *,
    force: bool = False,
    rebuild: bool = False,
    manifest_path: Path | None = None,
) -> None:
    """
    Interactive first-time workspace setup.

    Runs the setup wizard on first initialisation.  If the manifest already
    exists the wizard is skipped and managed files are refreshed — unless
    ``force=True``, which re-runs the full wizard.

    Pass ``manifest_path`` to skip the wizard entirely and use a pre-authored
    manifest file.  The manifest is baked into the Docker image as
    ``/etc/contained/manifest.yaml`` so hooks read policy from the image rather
    than from the workspace.  Suitable for CI/CD and reproducible builds.

    Pass ``rebuild=True`` to force a Docker image rebuild even when the image's
    version label already matches the installed contAIned version.
    """
    target = target.resolve()
    console.print(f"\n[bold]contAIned init[/bold] — [dim]{target}[/dim]\n")

    manifest_new = target / ".contAIned" / "manifest.yaml"
    manifest_old = target / ".contAIned" / "policy" / "manifest.yaml"
    already_init = manifest_new.exists() or manifest_old.exists()

    # Docker is the only supported runtime.
    docker_config = dict(_DEFAULT_DOCKER_CONFIG)

    if manifest_path is not None:
        # ── Non-interactive: use provided manifest ─────────────────────────────
        if not manifest_path.exists():
            console.print(f"[red]✗[/red] Manifest not found: {manifest_path}")
            raise SystemExit(1)
        try:
            import yaml as _yaml

            raw = manifest_path.read_text()
            _yaml.safe_load(raw)  # validate
            manifest_content: str | None = raw
        except Exception as exc:
            console.print(f"[red]✗[/red] Invalid manifest: {exc}")
            raise SystemExit(1)
        # Update docker_config from the provided manifest
        parsed: dict = {}
        try:
            import yaml as _yaml

            parsed = _yaml.safe_load(manifest_content) or {}
            if "runtime" in parsed and "docker" in parsed["runtime"]:
                docker_config.update(parsed["runtime"]["docker"])
        except Exception:
            pass
        console.print(f"  Using manifest: [dim]{manifest_path}[/dim]")
        try:
            image_rebuilt = _docker_setup(
                docker_config,
                target,
                rebuild=True,
                manifest_content=manifest_content,
                managed_settings_content=_build_managed_settings(parsed),
            )
        except RuntimeError as exc:
            console.print(f"\n[red]✗[/red] Docker setup failed: {exc}")
            raise SystemExit(1)

    elif already_init and not force:
        console.print("[dim]Workspace already initialised — refreshing managed files.[/dim]\n")
        # Re-bake the existing manifest into the image so it stays in sync.
        existing = manifest_new if manifest_new.exists() else manifest_old
        try:
            manifest_content: str | None = existing.read_text()
        except OSError:
            manifest_content = None
        if manifest_content is None:
            console.print(
                "[yellow]Warning:[/yellow] No manifest found at .contAIned/manifest.yaml.\n"
                "A policy manifest is required — running the setup wizard now.\n"
            )
            manifest_content = _run_wizard(docker_config)
        try:
            import yaml as _yaml

            _parsed_existing = _yaml.safe_load(manifest_content) or {}
            image_rebuilt = _docker_setup(
                docker_config,
                target,
                rebuild=rebuild,
                manifest_content=manifest_content,
                managed_settings_content=_build_managed_settings(_parsed_existing),
            )
        except RuntimeError as exc:
            console.print(f"\n[red]✗[/red] Docker setup failed: {exc}\n")
            raise SystemExit(1)

    else:
        if already_init and force:
            console.print(
                "[yellow]Re-running setup wizard (--force). "
                "Your current configuration will be replaced.[/yellow]\n"
            )
        console.print("Welcome to contAIned.\n")
        manifest_content = _run_wizard(docker_config)

        try:
            import yaml as _yaml

            _parsed_wizard = _yaml.safe_load(manifest_content) or {}
            image_rebuilt = _docker_setup(
                docker_config,
                target,
                rebuild=rebuild,
                manifest_content=manifest_content,
                managed_settings_content=_build_managed_settings(_parsed_wizard),
            )
        except RuntimeError as exc:
            console.print(f"\n[red]✗[/red] Docker setup failed: {exc}")
            raise SystemExit(1)

    # ── Sigstore signing ──────────────────────────────────────────────────────
    # Sign if: Sigstore enabled AND (image was rebuilt OR no provenance yet).
    # Skips re-signing on a refresh where the image was already up to date —
    # the existing Rekor entry is still valid.
    provenance_data: dict | None = None
    sigstore_enabled = False
    if manifest_content:
        try:
            import yaml as _yaml

            _parsed = _yaml.safe_load(manifest_content) or {}
            _sigstore = _parsed.get("sigstore", {})
            sigstore_enabled = bool(_sigstore.get("enabled", False))
            provenance_exists = (target / ".contAIned" / "provenance.yaml").exists()
            if sigstore_enabled and (image_rebuilt or not provenance_exists):
                from contained.sigstore import cosign_sign

                rekor_url = _sigstore.get("rekor_url", "https://rekor.sigstore.dev")
                fulcio_url = _sigstore.get("fulcio_url", "https://fulcio.sigstore.dev")
                image = docker_config.get("image", "contained:latest")
                bundle_dest = target / ".contAIned" / "provenance.bundle"
                console.print("  Signing image with Sigstore …", end="")
                try:
                    provenance_data = cosign_sign(image, rekor_url, fulcio_url, bundle_dest)
                    console.print(" [green]done[/green]")
                    # Write provenance.yaml now so the smoke-test below sees fresh data.
                    _write_provenance(target, provenance_data)
                    # Smoke-test: verify the provenance we just wrote
                    console.print("  Verifying provenance …", end="")
                    try:
                        from contained.verify import _verify_workspace

                        _verify_workspace(target)
                        console.print(" [green]ok[/green]")
                    except RuntimeError as verify_exc:
                        console.print(" [yellow]warning[/yellow]")
                        console.print(
                            f"  [yellow]Warning:[/yellow] post-sign verification failed: "
                            f"{verify_exc}"
                        )
                except Exception as exc:
                    console.print(" [yellow]warning[/yellow]")
                    console.print(
                        f"  [yellow]Warning:[/yellow] image signing failed — "
                        f"workspace will function but lacks Sigstore provenance.\n"
                        f"  {exc}"
                    )
        except Exception:
            pass

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
    # Overwrite on re-runs so that contAIned init refreshes hooks to the latest
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
        results.append((".contAIned/manifest.yaml", status))
    elif manifest_old.exists() and not manifest_new.exists():
        # Migrate old path → new path, preserving content
        manifest_new.parent.mkdir(parents=True, exist_ok=True)
        manifest_new.write_text(manifest_old.read_text())
        results.append((".contAIned/manifest.yaml", "migrated"))
    else:
        # manifest_new exists but manifest_content is None only in degenerate cases;
        # all normal paths set manifest_content before reaching here.
        results.append((".contAIned/manifest.yaml", "exists"))

    # ── Directory markers ─────────────────────────────────────────────────────
    for path in _markers(target):
        results.append((str(path.relative_to(target)), _touch(path)))

    # ── Provenance ────────────────────────────────────────────────────────────
    if provenance_data:
        results.append((".contAIned/provenance.yaml", _write_provenance(target, provenance_data)))
    elif sigstore_enabled:
        results.append((".contAIned/provenance.yaml", "failed"))
    else:
        results.append((".contAIned/provenance.yaml", "skipped (Sigstore disabled)"))

    # ── .claude/settings.json — strip duplicate hook registrations ───────────
    # Hooks are registered exclusively via managed-settings.json (image layer).
    # Any "hooks" key in .claude/settings.json causes double-firing; migrate it.
    results.append((".claude/settings.json", _migrate_settings_json(target)))

    # ── .gitignore ────────────────────────────────────────────────────────────
    if git_root:
        results.append((".gitignore", _update_gitignore(git_root)))

    _print_table(results)

    console.print(
        f"\n[bold]Workspace initialised.[/bold] "
        f"[dim]runtime: docker ({docker_config['image']})[/dim]"
    )
    console.print("  contAIned  # For REPL\n")
