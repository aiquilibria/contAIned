# contAIned — Test Suite

## Philosophy

### What we test

We test **pure logic and I/O-safe behaviour** — functions whose correctness can
be verified without a running Docker daemon, a live network, or an interactive
terminal:

- **Filesystem logic** — file creation, path walk-up, directory markers,
  idempotency, executable-bit setting (`init.py`, `cli.py`, `session.py`)
- **YAML / manifest handling** — building, merging, and reading manifests;
  shape-merge semantics; legacy-path fallback (`init.py`, `session.py`)
- **JSONL parsing** — transcript extraction functions that pair tool calls with
  their results, extract narrative text, and build session summaries (`tracer.py`)
- **Database logic** — SQLite schema, blob deduplication, baseline/snapshot
  lifecycle, diff generation, GC pruning, audit event storage (`tracer.py`)
- **Network policy logic** — domain allowlist matching, case normalisation,
  subdomain rules, HTTP header reading (`proxy.py`)
- **Socket-level behaviour** — request handling for blocked domains using
  `socket.socketpair()` so no real network is required (`proxy.py`)

### What we don't test

We do not write unit tests for code whose primary job is to **coordinate
external processes**:

- **Docker operations** — image builds, container runs, volume/network creation.
  These require a running daemon and are tested by running `contAIned` itself.
- **Interactive CLI prompts** — `click.confirm` / `click.prompt` flows inside
  `run_init`. Correctness here is better verified through manual or end-to-end
  testing.
- **`subprocess` calls to `claude`** — the `start_repl` local-mode path execs
  the Claude Code CLI. There is nothing to unit-test beyond the argument
  construction, which is trivial.
- **Rich console output** — `_print_splash`, `_print_runtime_banner`, and
  `_print_table` produce formatted terminal output. Their content is obvious
  from reading the code; testing it would just be asserting strings.

### What we mock (and what we don't)

**We avoid mocks wherever possible.** Real objects catch real bugs; mocks only
catch the bugs you imagined.

| Situation | Approach |
|---|---|
| Filesystem operations | `pytest`'s `tmp_path` fixture — a real temporary directory |
| SQLite database | Real in-memory-equivalent file in `tmp_path` |
| Git operations | Real `git init` in `tmp_path` |
| Socket I/O | `socket.socketpair()` — a real kernel pipe, no network |
| Error branches (e.g. `_get_tracer` failure) | `monkeypatch` to inject a raising class |

The one place we use `monkeypatch` for error injection is
`TestGetTracer.test_returns_none_gracefully_on_error` — specifically to verify
that `_get_tracer` silently returns `None` rather than propagating an exception.
Even there we patch at the class level, not at the import level, to stay as
close to real code paths as possible.

---

## Coverage summary

| Module | Cover | What is tested |
|---|---|---|
| `__init__.py` | 100% | (empty) |
| `templates.py` | 100% | All constants importable; values used by other tests |
| `tracer.py` | 94% | Schema, blob store, baselines, snapshots, diffs, GC, audit events, task lifecycle, narrative extraction, transcript pairing |
| `cli.py` | 69% | `_find_root` walk-up logic (all branches) |
| `session.py` | 52% | `_load_manifest`, `_load_model_config`, `_check_initialised`, `_get_tracer` |
| `proxy.py` | 52% | `_is_allowed`, `_read_request_head`, `_handle` blocked-domain paths |
| `init.py` | 47% | `_write_file`, `_touch`, `_is_git_repo`, `_git_root`, `_init_git_repo`, `_update_gitignore`, `_sync_manifest`, `_build_manifest`, `_managed_files`, `_contAIned_version` |
| `docker_runner.py` | 0% | — (see gaps below) |

---

## Gaps and justification

### `docker_runner.py` — 0%

Every function in this module either calls `docker` as a subprocess or manages
container lifecycle (run, attach, copy files, exec). There is no extractable
pure logic worth isolating. Correctness is validated by running the tool against
a real Docker daemon. Adding mocks here would test our ability to call
`subprocess.run` with the right arguments — not whether the container actually
works.

### `init.py` — `_docker_setup`, `run_init`, `_print_table` (53% uncovered)

`_docker_setup` shells out to `docker build`, `docker volume create`, and
`docker network create`. `run_init` is the interactive wizard that drives
`_docker_setup` and then delegates to all the functions that *are* tested.
`_print_table` formats a Rich table to the terminal. None of these have logic
that belongs in a unit test.

### `session.py` — `start_repl`, `_print_runtime_banner`, `_print_splash` (48% uncovered)

`start_repl` either delegates to `DockerRunner` or `exec`s the `claude` binary.
Both paths are integration-level behaviour. The argument-construction logic
(model flag, missing-files check) is thin and already covered indirectly through
`_check_initialised` and `_load_model_config` tests. The print helpers produce
terminal output only.

### `cli.py` — `main`, `init` command bodies (31% uncovered)

Both command bodies are one- or two-line delegators: `main` calls `start_repl`,
`init` calls `run_init`. The delegation itself is correct by construction;
testing it would require mocking both callees, leaving nothing real to assert.
The only logic worth testing — `_find_root` — is fully covered.

### `proxy.py` — `_relay`, allowed-domain tunnel/forward paths, `main` (48% uncovered)

`_relay` is a tight read/write loop between two sockets. It is correct when both
sides close cleanly, which only happens with a real remote endpoint. The
allowed-domain paths in `_handle` (CONNECT tunnel and plain HTTP forward) reach
out to `socket.create_connection` — again requiring a real remote. `main` binds
port 3128 and loops forever. All three are integration-level concerns tested by
running the proxy sidecar inside the container.

### `tracer.py` — 17 lines (6% uncovered)

The remaining uncovered lines are `OSError` / `except Exception` branches inside
transcript-reading functions — error paths that trigger only when a file
disappears between the existence check and the read, or when the SQLite
connection drops mid-write. These are defensive guards, not logic. They do not
warrant tests.
