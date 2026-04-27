# Contributing to contAIned

## Repo structure

```
cli/                        Go CLI binary (cobra): init, verify, REPL startup
  cmd/                      Cobra commands: root, init, verify
  internal/
    manifest/               Manifest parsing and merging
    scaffold/               Hook template embedding and scaffolding
      templates/            Hook scripts and CLAUDE.md baked into the Docker image
    pysource/               go:generate target — embeds Python source into the binary
cli/Makefile                Build targets
src/contained/              Python runtime: tracer DB, MCP server, payload assembly
  tracer.py                 SQLite audit database and content-addressed blob store
  tracer_mcp.py             MCP server that exposes tracer as read-only tools
  proof.py                  ATP proof assembly
  runtime/
    Dockerfile              Runtime image (Python 3.13, Node.js, Claude Code)
    statusline.py           Status line renderer
tests/                      pytest tests for tracer and QA hook logic
.contAIned/                 Active workspace control plane — do not edit
docs/                       User-facing reference documentation
docs/internal/              Internal design notes — not for end users
```

## Building

### Go CLI

```bash
cd cli
go build ./...          # build
go test ./...           # test
go vet ./...            # vet
make build              # build → dist/contained (current platform)
make build-all          # cross-compile: linux/darwin/windows × amd64/arm64
```

The CLI embeds Python source files and hook templates at build time via `go generate`. If you modify anything under `src/contained/` or `cli/internal/scaffold/templates/`, run:

```bash
cd cli
go generate contained.dev/cli/internal/pysource
```

before building. CI does this automatically.

### Python runtime

```bash
# From the repo root
pip install -e ".[dev]"   # or: pip install -e ".[dev]" --extra-index-url ...
pytest tests/             # run test suite
ruff check .              # lint
pyright .                 # type check
```

Python 3.13 is required. The test suite uses only the standard library and pytest — no Docker, no Claude Code.

### Docker image

The runtime image is built locally by `contAIned init`. To rebuild after changing `src/contained/runtime/Dockerfile` or `managed-settings.json`:

```bash
contained init --rebuild
```

## Making changes

### Go-only changes (CLI flags, manifest parsing, scaffold templates)

Edit, build, test as normal. If you change a hook template under `cli/internal/scaffold/templates/`, run `go generate` (see above) to re-embed it.

### Python-only changes (tracer, MCP server, proof assembly)

Edit the source under `src/contained/`. Run `pytest tests/` to verify. If your change is logic that also ships embedded in the CLI binary, run `go generate` in `cli/` and then `go test ./...` to confirm the embedding round-trips correctly.

### Hook scripts (`cli/internal/scaffold/templates/hooks/`)

Hook scripts are baked into the Docker image at `/etc/contained/hooks/` — they are not written to the workspace. Edit the templates in `cli/internal/scaffold/templates/hooks/`. After editing a template, run `go generate` in `cli/` to re-embed, then `contained init --rebuild` in a test workspace to verify the hooks take effect.

### Policy manifest (`manifest.yaml` / `.contAIned_manifest.yaml`)

`manifest.yaml` at the workspace root defines policy for the active `.contAIned/` workspace. It is not a general-purpose config file. If you are adding a new manifest field, the schema lives in `cli/internal/manifest/`.

## Tests

| Suite | Command | What it covers |
|-------|---------|----------------|
| Go unit + integration | `cd cli && go test ./...` | CLI commands, manifest parsing, scaffold embedding |
| Python unit + integration | `pytest tests/` | Tracer DB, blob store, QA hook logic |

Both suites run on every push and PR via GitHub Actions (`.github/workflows/ci.yml`). Pull requests must pass both.

## Pull requests

- Keep PRs focused. A bug fix and an unrelated refactor should be separate PRs.
- If you are changing hook templates or the managed CLAUDE.md, explain the behavioral intent in the PR description — these changes affect every agent session.
- If you are changing policy enforcement logic (hook scripts, tracer, QA), add or update tests.

## Architecture notes

- **Policy enforcement is in hooks, not the agent.** The CLAUDE.md baked into the Docker image (`/etc/claude-code/CLAUDE.md`) is behavioral guidance; actual enforcement happens in `PreToolUse`/`PostToolUse` hooks. Do not rely on CLAUDE.md for security properties.
- **The tracer is append-only by design.** `tracer.db` records baselines and snapshots; the diff is reconstructed on read. Do not add UPDATE or DELETE to the hot path.
- **The CLI embeds everything.** Hook scripts, Python source, the managed CLAUDE.md, and managed-settings.json are all embedded in the Go binary at build time. A released binary is fully self-contained.

## Reporting issues

Open an issue on GitHub. For security vulnerabilities, follow the process in [SECURITY.md](SECURITY.md).
