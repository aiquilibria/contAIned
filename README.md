# contAIned

A coding agent CLI built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview).

The agent operates within a defined workspace inside an isolated Docker container. All tool calls are audited. Policy is enforced by hook scripts before every tool call — the operator never needs to approve individual actions.

## Why contAIned?

Two existing primitives handle adjacent problems but leave a critical gap:

### Claude Code's `/sandbox`

Claude Code ships a built-in `/sandbox` feature that uses OS-level isolation (Seatbelt on macOS, bubblewrap on Linux) to constrain Bash subprocesses — blocking writes to sensitive paths, restricting outbound network access, and so on. It is useful, and contAIned can run alongside it.

What it does not cover: Claude's own SDK tool calls — `Write`, `Edit`, `Read`, `Glob`, `Grep` — are mediated entirely within the Claude Code process. They never cross the OS process boundary that `/sandbox` watches. A policy that says "deny writes to `.contAIned/`" in the sandbox has no effect on a `Write` tool call; only a `PreToolUse` hook does.

Beyond the coverage gap, `/sandbox` has no concept of:
- An audit log of what the agent did and why
- A task lifecycle with human review before changes are accepted
- Quality gates that block the agent from declaring success until checks pass
- Live, per-project policy that an operator can update mid-session

### Docker AI Sandboxes

[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) run agents inside lightweight microVMs with private Docker daemons, providing strong host-system isolation. The isolation guarantee is real and valuable.

The design philosophy is explicitly "YOLO mode by default — agents work without asking permission." It solves the question *"can the agent damage my host?"* and answers no. It does not try to answer *"what did the agent actually do, was it correct, and did a human sign off on it?"*

Additional constraints:
- MicroVM-based sandboxes require **macOS or Windows** (Linux users fall back to legacy container mode — the same isolation Docker has always provided).
- No per-tool-call policy hooks, no audit log, no operator review workflow, no QA gates.

### The gap both leave open

Neither primitive addresses the governance layer: *intercepting, logging, and selectively blocking individual agent tool calls, recording a content-addressed diff of every change, enforcing quality criteria before the agent can declare a task done, and requiring operator sign-off before work is accepted.*

contAIned fills that gap:

| Capability | `/sandbox` | Docker Sandbox | **contAIned** |
|---|:---:|:---:|:---:|
| Isolates agent from host filesystem | ◑ (subprocess only) | ✓ (microVM) | ✓ (Docker container) |
| Covers SDK tool calls (`Write`, `Edit`, `Read`) | ✗ | ✗ | ✓ (PreToolUse hooks) |
| Append-only audit log of every tool call | ✗ | ✗ | ✓ |
| Content-addressed diff store per task | ✗ | ✗ | ✓ |
| Operator review before changes are accepted | ✗ | ✗ | ✓ |
| QA gate blocks agent from finishing prematurely | ✗ | ✗ | ✓ |
| Live policy without container rebuild | ✗ | ✗ | ✓ |
| Works on Linux in CI/CD | ✓ | ✗ (MicroVM) | ✓ |

contAIned and `/sandbox` are complementary, not competing. Enabling both means subprocess writes are blocked at the OS level *and* SDK tool calls are blocked at the hook level — two independent enforcement layers from two different trust boundaries.

## Install

```bash
uv add contAIned
# or
pip install contAIned
```

## Quickstart

```bash
# 1. Go to your project
cd my-project

# 2. Initialise the contAIned workspace (builds the Docker image, wires hooks)
contAIned init

# 3. Start a session
contAIned
```

## Commands

### `contAIned`

Starts an interactive Claude Code session inside the contAIned container.

```bash
contAIned
```

Claude Code runs as a direct child process with your terminal inherited — all I/O passes through unmodified. The agent has access to your project workspace (bind-mounted at `/workspace`) and nothing else.

**Hash commands** (handled by the `UserPromptSubmit` hook before the agent sees them):

| Command | What it does |
|---|---|
| `#review` | List recent completed tasks |
| `#review <N>` | Show narrative + diff summary for task N |
| `#db` | Query `tracer.db` — last 10 tasks |
| `#db <SQL>` | Run arbitrary SQL against `tracer.db` |
| `#status` | Show last 20 audit-log entries |
| `#update` | Display the current manifest (YAML) |
| `#update <dotpath>=<value>` | Set a manifest value live (e.g. `#update policy.qa.lint=false`) |

Any other input is forwarded verbatim to the agent.

---

### `contAIned init [DIRECTORY]`

Scaffolds the contAIned workspace in the target directory (default: current directory).

```bash
contAIned init              # initialise in current directory
contAIned init ./myrepo     # initialise in a specific directory
contAIned init --force      # re-run setup wizard (reconfigure model, docker, etc.)
contAIned init --rebuild    # force-rebuild the Docker image without re-running wizard
```

Runs an interactive wizard:

1. **Docker configuration** — image name, network, resource limits.
2. **Policy options** — audit logging, git-mutation policy, rate limiting, default model.

Creates:

```
.contAIned/
  hooks/
    restrict_reads.py    ← PreToolUse: read path enforcement
    restrict_writes.py   ← PreToolUse: write path enforcement
    restrict_bash.py     ← PreToolUse: bash command restrictions
    audit.py             ← PostToolUse: append-only audit log
    qa.py                ← Stop: quality gate
  manifest.yaml          ← docker + policy + agent settings
  tracer.db              ← SQLite task + diff store (gitignored)
  audit/                 ← audit log (gitignored)

.claude/
  settings.json          ← Claude Code hook wiring + permission rules

CLAUDE.md                ← agent operating instructions
```

Re-running `contAIned init` without `--force` refreshes hook files from the latest bundled templates and syncs any new manifest keys, without overwriting your policy values.

---

## Task lifecycle

When the agent signals completion, QA checks run and a diff summary is built. The task is then marked `closed` automatically.

```
Agent signals Stop
        │
        ▼
  QA checks (qa.py) + diff summary + narrative built
        │
        ▼
  Task marked closed
```

Use `#review` at any time to browse completed tasks and read their narratives.

### Task states

| State    | Meaning                          |
|----------|----------------------------------|
| `open`   | Agent is actively working.       |
| `closed` | Turn complete; narrative stored. |

In a multi-turn session the task cycles: each new user message reopens it to `open`, and the Stop hook closes it again when the agent finishes that turn.

---

## How it works

### Isolation

The agent runs inside a Docker container. The workspace is bind-mounted at `/workspace`; the rest of the host filesystem is invisible. No path-bypass trick can reach files outside the workspace — the kernel enforces this.

### Governance

Three hook layers evaluate every tool call:

```
Tool call
    │
    ├── PreToolUse hook (restrict_writes.py / restrict_reads.py / restrict_bash.py)
    │     Path-based enforcement — deny access outside the workspace
    │
    ├── Deny rules (settings.json)
    │     Pattern-based — rm -rf, sudo, curl, git push, etc.
    │
    ├── Allow rules (settings.json)
    │     Pattern-based — Read, Glob, Grep, safe Bash patterns
    │
    └── canUseTool callback
          Anything not covered above — surfaces to the operator for approval
```

After every successful tool call, `audit.py` appends a structured log entry.
When the agent stops, `qa.py` runs syntax and quality checks — if they fail,
the agent receives feedback and keeps working.

### Defense-in-depth with Claude Code's sandbox

As explained in [Why contAIned?](#why-contained), Claude Code's `/sandbox` covers OS-level subprocess isolation while contAIned's PreToolUse hooks cover SDK tool calls — they protect different trust boundaries and work best together.

To enable both, add a `sandbox` block to `.claude/settings.json`:

```json
{
  "sandbox": {
    "enabled": true,
    "filesystem": {
      "denyWrite": [".contAIned"]
    }
  }
}
```

This prevents any Bash subprocess (shell scripts, Python executed via Bash, build tools, etc.) from writing to the `.contAIned/` control-plane directory at the OS level — a second line of defence behind the PreToolUse hook that already enforces this for `Write`/`Edit` calls.

### Tracer

`tracer.db` (SQLite, WAL mode) records every task, sub-agent invocation, file diff (content-addressed blob store), and QA result. The `#db` hash command gives the operator direct SQL access.

---

## Customising policy

Edit `.contAIned/manifest.yaml` to adjust what the agent can do, or use `#update <dotpath>=<value>` from within a session. Manifest changes are live — hooks read the manifest on every tool call.

Edit `.claude/settings.json` to add or remove allow/deny rules.

> **Do not edit hook files directly.** Files under `.contAIned/hooks/` are generated from internal templates. Running `contAIned init` (with or without `--rebuild`) will overwrite them, silently discarding any local changes. Policy customisation belongs in `manifest.yaml`; structural hook changes should be raised as feature requests.

---

## Known gaps

### QA hook coverage

The built-in `qa.py` Stop hook ships with quality checks for Python projects (linting, type checking, tests). There are no pre-built QA checks for other languages — Go, JavaScript, TypeScript, etc. Projects using those languages must write their own checks in `qa.py`, which requires Python knowledge and is not guided by the current tooling. Broader language coverage is planned.

### Garbage collection (`tracer.db`)

`tracer.db` grows without bound. The old `contAIned gc` CLI command has been removed along with all other subcommands; there is currently no way to trigger GC from within a session.

**Pending:** implement `#gc [--keep-days N]` as a hash command in the `UserPromptSubmit` hook. Until then, the database can be pruned manually:

```sql
-- connect via: #db
DELETE FROM blobs WHERE sha256 NOT IN (SELECT sha256 FROM diffs);
DELETE FROM diffs WHERE task_id IN (
  SELECT id FROM tasks WHERE created_at < unixepoch('now', '-30 days')
);
DELETE FROM tasks WHERE created_at < unixepoch('now', '-30 days');
VACUUM;
```
