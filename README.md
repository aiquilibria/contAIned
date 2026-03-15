# slash

A coding agent CLI built on the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview).

The agent operates within a defined workspace. All tool calls are governed by policy hooks before execution. Writes are restricted to the task output directory. Everything is audited.

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

# 2. Initialise the slash workspace
slash init

# 3. Run a task
slash run "Add docstrings to all functions in src/utils.py"

# 4. Review output
git diff

# 5. Check the audit log
slash status
```

## Commands

### `slash init [DIRECTORY]`

Scaffolds the slash workspace in the target directory (default: current directory).

Creates:
```
.slash/
  hooks/
    restrict_writes.py   ← PreToolUse: path enforcement
    audit.py             ← PostToolUse: append-only audit log
    qa.py                ← Stop: quality gate
  policy/
    manifest.yaml        ← allow/deny rules
  audit/                 ← audit log (gitignored)

.claude/
  settings.json          ← SDK hook wiring + permission rules

CLAUDE.md                ← agent operating instructions
```

Also updates `.gitignore` to exclude `.slash/audit/`.

### `slash run <task>`

Runs the agent on a task description. The agent:
- Reads freely from the project
- May write anywhere within the project root
- Has every tool call checked against `manifest.yaml` before execution
- Runs QA checks automatically when it signals completion

```bash
slash run "Add type annotations to all functions in auth.py"
slash run "Write pytest tests for the UserService class"
slash run "Refactor the database module to use async/await"
```

### `slash status`

Shows a summary of the audit log.

```bash
slash status
slash status --tail 50
```

### `slash update`

Refreshes managed hook files from the latest bundled templates. Safe to run after upgrading. Never touches user-editable files (e.g. `manifest.yaml`).

```bash
slash update
```

## How it works

Governance is implemented through three hook layers, evaluated in order on every tool call:

```
Tool call
    │
    ├── PreToolUse hook (restrict_writes.py)
    │     Path-based enforcement — deny writes outside the project root
    │
    ├── Deny rules (settings.json)
    │     Pattern-based — rm, sudo, curl, git push, etc.
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

## Customising policy

Edit `.slash/policy/manifest.yaml` to adjust what the agent can do.
Edit `.slash/hooks/qa.py` to add project-specific quality checks (linting, tests, etc.).
Edit `.claude/settings.json` to add or remove allow/deny rules.

The policy manifest is the source of truth for the `restrict_writes.py` hook.
The `settings.json` allow/deny rules are evaluated by the SDK directly, without
invoking a hook subprocess — so they're fast and appropriate for high-volume patterns.
