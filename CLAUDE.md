# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**contAIned** is a coding agent CLI built on Claude Code that runs inside an isolated Docker container with operator-controlled policy enforcement. It combines:
- Container-level isolation (Docker bind-mounts `/workspace`)
- Governance hooks (PreToolUse/PostToolUse/Stop) that enforce policy on every tool call
- An audit trail with content-addressed file diffs, QA gates, and operator review

**This repo is dogfooded** — contAIned is developed using itself. The `.contAIned/` directory at the repo root is the active workspace: the hooks, tracer DB, and audit log here govern the agent sessions used to build this very codebase. Changes to hook scripts or the tracer take effect in the next session automatically (no rebuild needed), but changes to `manifest.yaml` or the Dockerfile require `contained init --rebuild`.

## Repository layout

```
cli/                   Go CLI binary (cobra): init, verify, REPL startup
src/contained/         Python runtime: tracer DB, MCP server, payload assembly
  runtime/             statusline.py, plugin/
tests/                 pytest tests for tracer and QA hook logic
.contAIned/            Workspace control plane (hooks, audit log, tracer DB)
  hooks/               Hook scripts registered in managed-settings.json
docs/                  Policy reference, security model, internal design docs
cli/internal/scaffold/ Hook templates + CLAUDE.md baked into Docker image
```

## Commands

### Go CLI (`cli/`)

```bash
cd cli
go build ./...          # Build
go test ./...           # Test
go vet ./...            # Vet
make build              # Build → dist/contained (current platform)
make build-all          # Cross-compile: linux/darwin/windows × amd64/arm64
make release            # build-all + SHA-256 checksums
```

### Python runtime

```bash
pytest tests/           # Run test suite
pytest tests/test_tracer.py::TestTracerDB::test_foo  # Single test
ruff check .            # Lint
pyright .               # Type check
```

### Docker image

```bash
# Rebuild the runtime image after changing Dockerfile or managed-settings.json
contained init --rebuild
```

## Architecture

### Execution flow

1. `contained` CLI (`cli/cmd/root.go`) checks for `.contAIned/` workspace, then starts a Docker container running Claude Code.
2. `contained init` (`cli/cmd/init.go`) merges `manifest.yaml` from the workspace and any repo `.contAIned_manifest.yaml`, and builds the Docker image with policy baked in — including hook scripts at `/etc/contained/hooks/`.
3. Inside the container, Claude Code runs with hooks registered in `/etc/claude-code/managed-settings.json` (highest precedence, immutable from the agent's perspective).

### Policy enforcement

Hook scripts baked into the image at `/etc/contained/hooks/` run on every tool call:
- `restrict_reads/writes/bash.py` — block out-of-scope operations
- `tracer_pre/post.py` — capture file baselines and snapshots into SQLite
- `audit.py` — append every tool call to the audit log
- `qa.py` — gate the Stop event; block if QA checks fail
- `push_hook.py` — detect git pushes for ATP submission
- `user_prompt_submit.py` — detect operator shell escapes (`!`)

### Tracer database (`src/contained/tracer.py`)

SQLite with WAL mode and a content-addressed blob store (zlib-compressed). Key tables: `tasks`, `blobs`, `baselines`, `snapshots`, `audit_events`. The MCP server (`tracer_mcp.py`) exposes this as read-only tools to the in-session agent.

### Manifest resolution (`cli/internal/manifest/`)

`manifest.yaml` in the workspace root defines policy (secrets, bash allow/deny, network, QA, MCP rules, ecosystems). If the repo contains `.contAIned_manifest.yaml`, it is merged at `init` time. The merged result is baked into the Docker image via `managed-settings.json`.

### `cli/internal/scaffold/templates/CLAUDE.md`

This file is embedded into the Docker image and loaded as the agent's operating instructions for every session. It is **not** for developers — it instructs the coding agent running inside contAIned.

## Key configuration files

| File | Purpose |
|------|---------|
| `pyproject.toml` | Python package, pytest config, ruff/pyright settings |
| `cli/Makefile` | Go build targets |
| `cli/go.mod` | Go dependencies (cobra, sigstore-go, yaml, color) |
| `src/contained/runtime/Dockerfile` | Runtime image (Python 3.13, Node.js, Claude Code) |
| `.contAIned/manifest.yaml` | Active policy manifest for this workspace |
| `.github/workflows/ci.yml` | Build/test/vet on every push |
| `.github/workflows/release-cli.yml` | Cross-compile + Sigstore sign on version tags |

## Release process

Releases are triggered by pushing a version tag. GitHub Actions cross-compiles the CLI for 5 targets (`linux/{amd64,arm64}`, `darwin/{amd64,arm64}`, `windows/amd64`), signs each binary with Sigstore keyless signing, generates `checksums.txt`, and uploads all assets (including `install.sh`) to GitHub Releases.
