# slash

A coding agent CLI built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview).

The agent operates within a defined workspace inside an isolated Docker container. All tool calls are audited. Policy is enforced by hook scripts before every tool call — the operator never needs to approve individual actions.

## Install

```bash
uv add slash
# or
pip install slash
```

## Quickstart

```bash
# 1. Go to your project
cd my-project

# 2. Initialise the slash workspace (builds the Docker image, wires hooks)
slash init

# 3. Start a session
slash
```

## Commands

### `slash`

Starts an interactive Claude Code session inside the slash container.

```bash
slash
```

Claude Code runs as a direct child process with your terminal inherited — all I/O passes through unmodified. The agent has access to your project workspace (bind-mounted at `/workspace`) and nothing else.

**Hash commands** (handled by the `UserPromptSubmit` hook before the agent sees them):

| Command | What it does |
|---|---|
| `#review` | List tasks awaiting review |
| `#review <N>` | Show diff + approve task N |
| `#db` | Query `tracer.db` — last 10 tasks |
| `#db <SQL>` | Run arbitrary SQL against `tracer.db` |
| `#status` | Show last 20 audit-log entries |
| `#sh <cmd>` | Run a shell command without involving the agent |
| `#update` | Display the current manifest (YAML) |
| `#update <dotpath>=<value>` | Set a manifest value live (e.g. `#update policy.qa.lint=false`) |

Any other input is forwarded verbatim to the agent.

---

### `slash init [DIRECTORY]`

Scaffolds the slash workspace in the target directory (default: current directory).

```bash
slash init              # initialise in current directory
slash init ./myrepo     # initialise in a specific directory
slash init --force      # re-run setup wizard (reconfigure model, docker, etc.)
slash init --rebuild    # force-rebuild the Docker image without re-running wizard
```

Runs an interactive wizard:

1. **Docker configuration** — image name, network, resource limits.
2. **Policy options** — audit logging, git-mutation policy, rate limiting, default model.

Creates:

```
.slash/
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

Re-running `slash init` without `--force` refreshes hook files from the latest bundled templates and syncs any new manifest keys, without overwriting your policy values.

---

## Review flow

Every task goes through an operator review when the agent signals completion.

```
Agent signals Stop
        │
        ▼
  QA checks (qa.py) + diff summary built
        │
        ▼
  #review          → list pending tasks
  #review <N>      → show diff for task N + approve
```

**Approve** — `#review <N>` marks the task `closed`.

**Follow-up** — type any instruction and press Enter. The agent wakes up with your message and keeps working in the same session.

### Task states

| State            | Meaning                                                     |
|------------------|-------------------------------------------------------------|
| `open`           | Agent is actively working.                                  |
| `pending_review` | Agent signalled Stop; awaiting operator review.             |
| `closed`         | Operator approved. Task is complete.                        |

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

Claude Code has a built-in `/sandbox` feature that uses OS-level isolation (Seatbelt on macOS, bubblewrap on Linux) to restrict what Bash subprocesses can read or write. You can enable it alongside slash for an extra layer of protection.

Add a `sandbox` block to `.claude/settings.json`:

```json
{
  "sandbox": {
    "enabled": true,
    "filesystem": {
      "denyWrite": [".slash"]
    }
  }
}
```

**What this adds:** prevents any Bash subprocess (shell scripts, Python files executed via Bash, build tools, etc.) from writing to the `.slash/` control-plane directory at the OS level.

**What it does not replace:** the sandbox only constrains processes that cross the OS process boundary. Claude's own `Write` and `Edit` tool calls are SDK-mediated and never cross that boundary — they are invisible to the sandbox. Slash's PreToolUse hooks (`restrict_writes.py`, `restrict_reads.py`, `restrict_bash.py`) remain the authoritative gate for those calls.

In short: sandbox covers subprocess writes; slash hooks cover SDK tool calls. Enabling both means a compromised subprocess and a misbehaving tool call each face an independent enforcement layer.

### Tracer

`tracer.db` (SQLite, WAL mode) records every task, sub-agent invocation, file diff (content-addressed blob store), and QA result. The `#db` hash command gives the operator direct SQL access.

---

## Customising policy

Edit `.slash/manifest.yaml` to adjust what the agent can do, or use `#update <dotpath>=<value>` from within a session. Manifest changes are live — hooks read the manifest on every tool call.

Edit `.slash/hooks/qa.py` to add project-specific quality checks (linting, tests, etc.). Hook file changes require a container rebuild (`slash init --rebuild`).

Edit `.claude/settings.json` to add or remove allow/deny rules.

---

## Known gaps

### Garbage collection (`tracer.db`)

`tracer.db` grows without bound. The old `slash gc` CLI command has been removed along with all other subcommands; there is currently no way to trigger GC from within a session.

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
