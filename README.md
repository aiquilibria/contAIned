# slash

A coding agent CLI built on the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview).

The agent operates within a defined workspace. `slash init` lets you choose between **local mode** (hook-enforced policy) and **Docker mode** (kernel-enforced filesystem isolation + hook policy). All tool calls are audited.

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

Runs an interactive wizard with three phases:

1. **Runtime selection** — choose `local` (hook-based policy) or `docker`
   (kernel-enforced filesystem isolation, recommended).
2. **Docker configuration** — if Docker mode is selected, `slash init` builds
   the `slash:latest` image, creates the `slash-agent-config` named volume, and
   creates the `slash-net` bridge network automatically.
3. **Manifest options** — audit logging, git-mutation policy, rate limiting, and
   default model.

Creates:
```
.slash/
  hooks/
    restrict_reads.py    ← PreToolUse: read path enforcement
    restrict_writes.py   ← PreToolUse: write path enforcement
    restrict_bash.py     ← PreToolUse: bash command restrictions
    audit.py             ← PostToolUse: append-only audit log
    qa.py                ← Stop: quality gate
  manifest.yaml          ← runtime + policy + agent settings
  audit/                 ← audit log (gitignored)

.claude/
  settings.json          ← SDK hook wiring + permission rules

CLAUDE.md                ← agent operating instructions
```

Also updates `.gitignore` to exclude `.slash/`.

### `slash run <task>`

Runs the agent on a task description. The agent:
- Reads freely from the project
- May write anywhere within the project root
- Has every tool call checked against `manifest.yaml` before execution
- In Docker mode, filesystem access is also bounded by the container runtime
  (kernel namespaces + bind mounts) — path-bypass tricks cannot circumvent it
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

### `slash repl`

Start an interactive REPL session. Every message you type is forwarded to the **same living agent session** — conversation history accumulates turn-by-turn so the agent remembers prior context within the session.

```bash
slash repl
slash repl --verbosity concise
```

Built-in commands (handled locally, never sent to the agent):

| Command | What it does |
|---|---|
| `/new` | Start a fresh session (new `session_id`, empty history) |
| `/sh <cmd>` | Run a shell command directly without involving the agent (e.g. `/sh git status`) |
| `/status` | Show the last 20 audit-log entries |
| `/help` | Print the built-in command list |
| `/clear` | Clear the terminal |
| `/exit` · `/quit` | Exit the REPL (also: Ctrl-D) |

Any other input is forwarded verbatim to the agent. All hooks, policy rules, and audit logging work identically to `slash run`.

### `slash update`

Refreshes managed hook files from the latest bundled templates. Safe to run after upgrading. Never touches user-editable files (e.g. `manifest.yaml`).

```bash
slash update
```

## Review flow

Every task goes through an operator review when the agent signals completion.

```
Agent signals Stop
        │
        ▼
  QA checks (qa.py) + diff summary built
        │
        ▼
  Press Enter to approve — or type a follow-up instruction and press Enter
        │
        ├─── blank line / Enter ──────────────────► task closed, session ends
        │
        └─── follow-up instruction ──────────────► agent continues same session
                                                    (loops back when it stops again)
```

**Approve** — press Enter on a blank line (or send `a` in CI / non-interactive
mode).  The task is marked `closed` and the session ends cleanly.

**Follow-up** — type any instruction and press Enter.  The agent wakes up with
your message and keeps working in the same session (same conversation history).
When it next signals Stop, the review prompt appears again.

There is no separate "dismiss" or "abandon" action in the normal flow.  If you
want to discard a task you can send the instruction `"abandon this"` or close
the session externally.

### Task states

| State            | Meaning                                                     |
|------------------|-------------------------------------------------------------|
| `open`           | Agent is actively working (or waiting for a follow-up).     |
| `pending_review` | Agent signalled Stop; operator review is in progress.       |
| `closed`         | Operator approved. Task is complete. Session is over.       |

---

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

Edit `.slash/manifest.yaml` to adjust what the agent can do.
Edit `.slash/hooks/qa.py` to add project-specific quality checks (linting, tests, etc.).
Edit `.claude/settings.json` to add or remove allow/deny rules.

The `runtime:` section of `manifest.yaml` is written by `slash init` and controls
whether the agent runs locally or inside a Docker container.  Do not edit it by hand
after `slash init` has written it — use `slash init` to re-initialise if you need to
change the runtime mode.

The `policy:` section is the source of truth for all hook enforcement rules.
The `settings.json` allow/deny rules are evaluated by the SDK directly, without
invoking a hook subprocess — so they're fast and appropriate for high-volume patterns.
