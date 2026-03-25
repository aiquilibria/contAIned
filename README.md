# <span style="color: #64CE91">cont[</span><span style="color: #BD3F39">AI</span><span style="color: #64CE91">]ned</span>
## <span style="color: #64CE91">[</span><span style="color: #BD3F39">✦</span><span style="color: #64CE91">] </span><span style="color: gray">take back control of your agent!</span>

A coding agent CLI built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview).

The agent operates within a defined workspace inside an isolated Docker container. All tool calls are audited. Policy is baked into the container image at build time and enforced by hook scripts before every tool call — the agent cannot change the rules it operates under. High-risk actions (such as git mutations) can be configured to escalate to the operator for explicit approval rather than being allowed or blocked outright.

## Contents

- [Why contAIned?](#why-contained)
- [Install](#install)
- [Quickstart](#quickstart)
- [Commands](#commands)
  - [`contAIned`](#contained-1)
  - [`contAIned init`](#contained-init-directory)
  - [`contAIned verify`](#contained-verify-directory)
- [Task lifecycle](#task-lifecycle)
- [How it works](#how-it-works)
  - [Isolation](#isolation)
  - [Governance](#governance)
  - [Defense-in-depth with Claude Code's sandbox](#defense-in-depth-with-claude-codes-sandbox)
  - [Egress filtering](#egress-filtering)
  - [Tracer](#tracer)
- [Customizing policy](#customizing-policy)
- [Known gaps](#known-gaps)
  - [QA hook coverage](#qa-hook-coverage)
  - [Garbage collection](#garbage-collection-tracerdb)
  - [Narrative injection budget](#narrative-injection-budget)
  - [Egress filtering against a malicious agent](#egress-filtering-against-a-malicious-agent)
- [Security model](#security-model)

## Why contAIned?

Two existing primitives handle adjacent problems but leave a critical gap:

### Claude Code's `/sandbox`

Claude Code ships a built-in `/sandbox` feature that uses OS-level isolation (Seatbelt on macOS, bubblewrap on Linux) to constrain Bash subprocesses — blocking writes to sensitive paths, restricting outbound network access, and so on. It is useful, and contAIned can run alongside it.

What it does not cover: Claude's own SDK tool calls — `Write`, `Edit`, `Read`, `Glob`, `Grep` — are mediated entirely within the Claude Code process. They never cross the OS process boundary that `/sandbox` watches. A policy that says "deny writes to `.contAIned/`" in the sandbox has no effect on a `Write` tool call; only a `PreToolUse` hook does.

Beyond the coverage gap, `/sandbox` has no concept of:
- An audit log of what the agent did and why
- A task lifecycle with human review before changes are accepted
- Quality gates that block the agent from declaring success until checks pass
- Operator-controlled policy that the agent itself cannot modify

### Docker AI Sandboxes

[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) run agents inside lightweight microVMs with private Docker daemons, providing strong host-system isolation. The isolation guarantee is real and valuable.

The design philosophy is explicitly "YOLO mode by default — agents work without asking permission." It solves the question *"can the agent damage my host?"* and answers no. It does not try to answer *"what did the agent actually do, was it correct, and did a human sign off on it?"*

Additional constraints:
- MicroVM-based sandboxes require **macOS or Windows** (Linux users fall back to legacy container mode — the same isolation Docker has always provided).
- No per-tool-call policy hooks, no audit log, no operator review workflow, no QA gates.

### The gap both leave open

Neither primitive addresses the governance layer: *intercepting, logging, and selectively blocking individual agent tool calls, recording a content-addressed diff of every change, enforcing quality criteria before the agent can declare a task done, and requiring operator sign-off before work is accepted.*

Neither addresses outbound network control either. An agent can exfiltrate workspace data via `WebFetch`, a Bash `curl`, or a Python script it writes and then executes — three different channels, each bypassing a different set of controls. `/sandbox` blocks some Bash network tools but not Claude Code's own `WebFetch`. Docker Sandboxes place no constraints on outbound traffic at all.

contAIned fills that gap:

| Capability | `/sandbox` | Docker Sandbox | **contAIned** |
|---|:---:|:---:|:---:|
| Isolates agent from host filesystem | ◑ (subprocess only) | ✓ (microVM) | ✓ (Docker container) |
| Covers SDK tool calls (`Write`, `Edit`, `Read`) | ✗ | ✗ | ✓ (PreToolUse hooks) |
| Append-only audit log of every tool call | ✗ | ✗ | ✓ |
| Content-addressed diff store per task | ✗ | ✗ | ✓ |
| Operator review before changes are accepted | ✗ | ✗ | ✓ |
| QA gate blocks agent from finishing prematurely | ✗ | ✗ | ✓ |
| Policy baked into image; tamper-proof at runtime | ✗ | ✗ | ✓ |
| Egress filtering — outbound network allowlist | ✗ | ✗ | ◑ (sandbox network + operator approval flow; prevents accidental exfiltration) |
| Works on Linux in CI/CD | ✓ | ✗ (MicroVM) | ✓ |

contAIned and `/sandbox` are complementary, not competing. Enabling both means subprocess writes are blocked at the OS level *and* SDK tool calls are blocked at the hook level — two independent enforcement layers from two different trust boundaries.

### Why not native settings?

Claude Code ships a real operator control plane: `managed-settings.json` accepts `allow`, `ask`, and `deny` rules, sandbox filesystem and network constraints, hook registration, and MCP server allowlists. contAIned uses all of these. The question is what they can and cannot express on their own.

**What native settings can approximate (cumbersomely)**

Simple path-based blocking for `Read`, `Write`, and `Glob` is mechanically expressible. Blocking `.env` files requires enumerating each pattern — `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `credentials.json`, … — across each tool separately: `Read`, `Glob`, `Grep`, `Write`, `Edit`, `MultiEdit`. That's upwards of 60 individual `deny` entries to cover the same ground as one Python regex. The same brittleness applies to Bash command restrictions: `deny: ["Bash(rm -rf*)"]` works for the literal prefix but misses variations, and has no way to check whether a `cat` command is targeting a sensitive file versus a source file.

**Where native settings cannot reach**

The deeper problems are structural:

- **`deny` beats `allow` unconditionally.** Claude Code's evaluation order is fixed: `deny → ask → allow`. There is no way to write "deny `**/.env*` except `**/.env.example`" — the deny fires regardless of any allow rule. The hooks resolve this with an explicit safe-variant check before the block logic.

- **No programmable logic.** Native rules are pattern lists. They cannot parse a bash command to extract its file argument, call an external tool, query a database, or apply context-sensitive judgement. The `restrict_bash.py` hook uses `shlex.split` to extract file paths from commands and checks those paths against the secret pattern list; no combination of native rules can replicate this.

- **No audit trail.** Native denials are silent — nothing is written. Hooks write a structured entry to `.contAIned/audit/pipeline.jsonl` and `tracer.db` on every denial and every allowed call, with timestamp, session ID, tool, target, and reason. That record exists whether or not the operator is watching, and is queryable via `#db`.

- **No lifecycle or quality gate.** There is no native equivalent of the `Stop` hook event. The permission system decides whether individual tool calls are allowed; it has no concept of intercepting the agent's "I am done" signal to run tests, check coverage, or build a diff summary before the result is accepted.

- **No tamper-proof policy.** A settings file in `.claude/settings.json` is writable by the agent. `managed-settings.json` in the image layer is harder to reach, but it is a static file with no provenance. contAIned bakes policy into the image at build time, records a manifest hash in the image label for drift detection, and optionally signs the image with Sigstore — giving operators a chain of custody from manifest authoring to runtime enforcement that native settings alone cannot provide.

- **No workspace isolation.** Native settings constrain what the agent *can do* but not *where it runs*. There is no settings-based equivalent of running the agent inside a Docker container with the workspace bind-mounted and nothing else visible.

**The design choice**

contAIned uses native settings for what they do well — hook registration, sandbox filesystem and network rules, WebFetch domain allowlisting, MCP server restrictions — and hooks for everything that requires logic, state, or lifecycle control. The split is deliberate: declarative rules for coarse structure, executable Python for anything that needs to reason about context.

| | Native settings | contAIned |
|---|:---:|:---:|
| Block reads/writes to specific path patterns | ✓ (verbose) | ✓ |
| Safe-variant exception (allow `.env.example`, deny `.env`) | ✗ | ✓ |
| Bash semantic parsing (detect `cat .env` vs `cat README.md`) | ✗ | ✓ |
| Structured audit log of every tool call | ✗ | ✓ |
| QA gate — block agent from finishing until checks pass | ✗ | ✓ |
| Actionable denial feedback to the agent | ✗ | ✓ |
| Task lifecycle, diff store, operator review | ✗ | ✓ |
| Policy tamper-proof at runtime; Sigstore provenance | ✗ | ✓ |
| Workspace isolation (agent sees only the project) | ✗ | ✓ |

---

## Install

**macOS / Linux — Homebrew:**

```bash
brew install lab-v2/tap/contained
```

**macOS / Linux — curl installer:**

```bash
curl -fsSL https://github.com/lab-v2/contAIned/releases/latest/download/install.sh | sh
```

**Direct download:** grab the binary for your platform from [GitHub Releases](https://github.com/lab-v2/contAIned/releases), make it executable, and place it on your `$PATH`.

The `contained` binary is a single self-contained executable with no runtime dependencies beyond Docker.

**Prerequisites:** Docker must be installed and running. `cosign` is optional — required only if you enable build provenance (Sigstore) during `contAIned init`. Install cosign: https://docs.sigstore.dev/cosign/system_config/installation/

## Quickstart

```bash
# 1. Go to your project
cd my-project

# 2. Write a manifest (see docs/policy-reference.md for the full schema)
cat > policy.yaml << 'EOF'
agent:
  model: claude-sonnet-4-6
policy:
  network:
    enabled: true
    allowed_domains:
      - api.anthropic.com
      - code.claude.com
EOF

# 3. Initialize the contAIned workspace
#    contAIned init builds the Docker image locally from the embedded Dockerfile —
#    there is no image to pull. This step requires a network connection to install
#    Claude Code and any toolchains resolved from declared ecosystems.
contAIned init --manifest policy.yaml

# 4. Start a session
contAIned
```

## Commands

### `contAIned`

Starts an interactive Claude Code session inside the contAIned container.

```bash
contAIned
```

Claude Code runs as a direct child process with your terminal inherited — all I/O passes through unmodified. The agent has access to your project workspace (bind-mounted at `/workspace`) and nothing else.

All input is forwarded verbatim to the agent. Use the **`/contained:tracer`** skill to query session history, audit logs, file diffs, and the tracer database directly.

---

### `contAIned init [DIRECTORY]`

Scaffolds the contAIned workspace in the target directory (default: current directory).

```bash
contAIned init --manifest policy.yaml                      # bake a local manifest into the image
contAIned init --mainlined https://mainlined.example.com   # fetch manifest from a mAInlined policy URL
contAIned init ./myrepo --manifest policy.yaml             # initialize in a specific directory
contAIned init --manifest policy.yaml --force              # re-initialise an existing workspace
contAIned init --manifest policy.yaml --rebuild            # force-rebuild the Docker image
contAIned init --ecosystem go                              # print a Go repo manifest starter and exit
```

A manifest must be provided via `--manifest` (local file) or `--mainlined` (mAInlined URL). Running without either flag prints a starter manifest and exits. See [docs/policy-reference.md](docs/policy-reference.md) for the full manifest schema.

> **Note:** `--mainlined` requires the `m<AI>nlined` companion policy service, which is currently in development and not yet publicly available. Use `--manifest` with a local file for all current deployments.

#### Repo manifest

Repositories may commit a `.contAIned_manifest.yaml` at the repo root that declares ecosystems and QA checks. This file is merged into the mAInlined manifest at `contained init` time — the image is the single merged artifact. Only two fields are permitted; any others cause `contained init` to fail.

```yaml
# .contAIned_manifest.yaml — committed to the repository root
ecosystems:
  go: "1.22.5"    # installs Go toolchain + adds proxy.golang.org to allowlist

policy:
  qa:
    checks:
      - name: test
        command: [go, test, ./...]
        when_changed: ["*.go"]
```

Each ecosystem key is resolved against `ecosystem_definitions` in the manifest passed to `contained init`. The resolved toolchain is installed and the required network domains are automatically added to the allowlist — no manual network config needed.

Use `--ecosystem` to print a pre-filled starter for your stack:

```bash
contAIned init --ecosystem go         > .contAIned_manifest.yaml
contAIned init --ecosystem node       > .contAIned_manifest.yaml
contAIned init --ecosystem python     > .contAIned_manifest.yaml
contAIned init --ecosystem typescript > .contAIned_manifest.yaml
```

See [docs/policy-reference.md](docs/policy-reference.md) for the full schema and version constraint rules.

Bakes the manifest into the Docker image at build time — policy is enforced at the image layer.

**Policy is enforced at the image layer.** Hook registration and sandbox rules live in `/etc/claude-code/managed-settings.json`, which is copied into the Docker image at build time. Claude Code treats this file as operator-managed policy: hooks registered there cannot be overridden or removed by the agent at runtime. The Mainlined manifest is baked into `/etc/contained/manifest.yaml` inside the image; hooks read policy parameters from that path exclusively.

Creates (in the workspace):

```
.contAIned/
  hooks/
    restrict_reads.py    ← PreToolUse: read path enforcement
    restrict_writes.py   ← PreToolUse: write path enforcement
    restrict_bash.py     ← PreToolUse: bash command restrictions
    audit.py             ← PostToolUse: append-only audit log
    qa.py                ← Stop: quality gate
  manifest.yaml          ← source of truth; baked into the image at build time
  tracer.db              ← SQLite task + diff store (gitignored)
  audit/                 ← audit log (gitignored)

CLAUDE.md                ← agent operating instructions
```

Baked into the Docker image (not in the workspace):

```
/etc/claude-code/
  managed-settings.json  ← hook registration + sandbox rules (highest-precedence settings level)
/etc/contained/
  manifest.yaml          ← policy parameters read by hooks at runtime
  statusline.py          ← status bar script
```

Re-running `contAIned init` without `--force` refreshes hook files to the latest bundled templates without touching your manifest. Use `--rebuild` or `--manifest` to rebuild the image.

---

### `contAIned verify [DIRECTORY]`

Verifies workspace image provenance before starting a session. Only meaningful when Sigstore was enabled during `contAIned init`; exits cleanly if provenance was disabled.

```bash
contAIned verify          # verify current workspace
contAIned verify ./repo   # verify a specific workspace
```

Checks:
1. The current `contained:latest` image digest matches the digest recorded at init time — detects image replacement between sessions.
2. The Sigstore signature in the Rekor transparency log is still valid for the recorded operator identity.

Requires `docker` on the host. Run this before `contAIned` when operating in environments where image integrity matters.

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

Every tool call passes through three layers, all registered in the image-layer managed settings and therefore unmodifiable by the agent:

```
Tool call
    │
    ├── PreToolUse hook (restrict_writes.py / restrict_reads.py / restrict_bash.py)
    │     Path-based enforcement — deny access outside the workspace;
    │     block writes to control-plane files (.contAIned/, managed-settings.json)
    │
    ├── Deny rules (managed-settings.json)
    │     Pattern-based — rm -rf, sudo, curl, git push, etc.
    │
    ├── Allow rules (managed-settings.json)
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

contAIned enables both automatically. The `managed-settings.json` baked into the image includes a `sandbox` block:

```json
{
  "sandbox": {
    "enabled": true,
    "filesystem": {
      "denyWrite": [".contAIned", ".claude/settings.json"]
    }
  }
}
```

This prevents any Bash subprocess (shell scripts, Python executed via Bash, build tools, etc.) from writing to the `.contAIned/` control-plane directory or the Claude Code settings file at the OS level — a second enforcement layer behind the PreToolUse hooks that already block these writes at the SDK level. No manual configuration is required.

### Egress filtering

An agent session has multiple channels for sending data out of the workspace: Claude Code's built-in `WebFetch` tool, Bash subprocesses (`curl`, `wget`), and scripts the agent writes and then executes. Different controls cover different channels.

contAIned addresses this with two complementary mechanisms, both driven by `policy.network.allowed_domains` in `manifest.yaml`:

- **`WebFetch` / `WebSearch`** — requests to allowed domains are auto-approved. Requests to any other domain surface an operator confirmation prompt (via the `PermissionRequest` hook) and are logged. No request proceeds without either an explicit allow rule or operator approval.
- **Bash subprocesses and agent-written scripts** — Claude Code's built-in sandbox enforces the `allowedDomains` list at the OS level (bubblewrap on Linux). HTTP traffic to non-allowed domains is blocked with a `403 Forbidden` regardless of the tool used.

Configure the allowlist in `.contAIned/manifest.yaml`, then rebuild the image:

```yaml
policy:
  network:
    enabled: true
    allowed_domains:
      - api.anthropic.com    # required — Anthropic API
      - code.claude.com      # Claude Code telemetry / auth
      - docs.anthropic.com   # documentation lookups
      # - pypi.org           # add project-specific domains as needed
```

```bash
contAIned init --rebuild
```

**Design intent — accidental exfiltration.** The sandbox network constraints cover Bash subprocesses and agent-written scripts at the OS level. `WebFetch` to non-allowed domains requires explicit operator approval rather than proceeding silently. Together these prevent the agent from accidentally sending data outside the workspace. The residual risk — raw non-HTTP socket connections that bypass both layers — is described in [Known gaps](#known-gaps) below.

### Policy hierarchy

contAIned separates two distinct governance concerns that are often conflated:

| Concern | What it controls | Risk addressed | Manifest key |
|---|---|---|---|
| **Dependency governance** (TPRM / SCA / SBOM) | What toolchains and package ecosystems the agent may install | Supply chain — installing vulnerable or unapproved software | `ecosystem_definitions`, `runtime.docker.toolchains` |
| **Egress governance** | What outbound domains the agent and its subprocesses may reach | Exfiltration — sending workspace data outside the project | `policy.network.allowed_domains` |

Both concerns are owned exclusively by the manifest passed to `contained init`. Ecosystem declarations in the repo manifest are resolved against `ecosystem_definitions` in that manifest — a team cannot install a toolchain or reach a package registry that the manifest has not approved.

**Lifecycle management — org floors and team pins**

The intended enforcement model for organizations is `m<AI>nlined` (the companion policy service, currently in development): a centrally-managed manifest distributed to teams, ensuring every workspace initialises against the same approved baseline. Until mAInlined is available, the same model is achievable by sharing a `policy.yaml` externally and having teams reference it at `contained init` time (`contained init --manifest policy.yaml`).

> **Note:** Organizational floor constraints are enforced externally — at `contained init` time, before the image is built. There is no runtime mechanism for a team to bypass them from inside the container.

The centrally-owned manifest sets minimum acceptable toolchain versions as floor constraints. Individual teams pin their required version within those bounds:

```yaml
# Centrally-owned manifest (distributed via mAInlined or a shared policy.yaml)
runtime:
  docker:
    toolchains:
      go: ">=1.22"      # floor: 1.22 or later; any version below fails at init
      python: ">=3.13"  # floor: Python 3.13+
      node: ">=18"

ecosystem_definitions:
  go:
    toolchain: go
    network_domains: [proxy.golang.org, sum.golang.org]
  python:
    network_domains: [pypi.org, files.pythonhosted.org]
```

```yaml
# Repo manifest (.contAIned_manifest.yaml) — owned by each team
ecosystems:
  go: "1.22.5"      # satisfies >=1.22 ✓
  python: "3.13.1"  # satisfies >=3.13 ✓
```

A version below the floor fails immediately with an actionable error — no image is built, no container runs:

```
ecosystem "go": version "1.21.0" does not satisfy constraint ">=1.22" for toolchain "go"
```

This gives the organization simultaneous control over two things: the minimum toolchain versions that satisfy compliance requirements, and the package registries those toolchains are permitted to contact. Teams cannot reach unapproved registries even if they declare a valid ecosystem, because `ecosystem_definitions` is owned entirely by the centrally-managed manifest.

---

### Tracer

`tracer.db` (SQLite, WAL mode) records every task, sub-agent invocation, file diff (content-addressed blob store), and QA result. Use the **`/contained:tracer`** skill to query session history, audit logs, and file diffs.

#### Provenance stamping

Every task closure writes a `provenance_log` entry into `tasks.summary`. Each entry captures the image digest, Sigstore operator identity, Rekor log index, and a `closed_at` timestamp — binding the task record to the exact signed image that enforced its policy.

This matters for accountability in two ways:

**Any task in the database is fully traceable.** Given a closed task, you can determine not just *what* the agent did (audit log + diffs) but *which signed image constrained it* — and from there independently verify the operator identity and policy contents via Rekor. The chain is: task → image digest → Rekor entry → operator OIDC identity → policy manifest.

**Resumed tasks accumulate a complete signing history.** When a task is reopened after a container rebuild (new `contAIned init`, new image signing), the next closure appends a second entry to `provenance_log` rather than overwriting the first. A task resumed across three builds carries three entries — each recording the image and operator identity that governed that portion of the work. The complete chain of custody is preserved regardless of how many rebuild cycles occurred.

The log is queryable directly:

```sql
-- Which image ran this task?
SELECT json_extract(summary, '$.provenance_log[0].image_digest'),
       json_extract(summary, '$.provenance_log[0].operator_identity'),
       json_extract(summary, '$.provenance_log[0].rekor_log_index')
FROM   tasks WHERE session_id = ?;

-- Tasks that ran under a specific image digest
SELECT session_id, started_at
FROM   tasks
WHERE  json_extract(summary, '$.provenance_log[0].image_digest') = 'sha256:...';
```

When Sigstore is disabled (`sigstore.enabled: false`), `provenance_log` is written as an empty list — the field is always present, making the absence of provenance legible rather than ambiguous.

---

## Customizing policy

Policy is baked into the Docker image at build time. To change it, edit `.contAIned/manifest.yaml` and rebuild:

```bash
contAIned init --rebuild                  # rebuild with existing manifest
contAIned init --manifest policy.yaml     # rebuild with a new manifest
```

Use `#policy` from within a session to view the effective policy (read-only).

The image is automatically rebuilt when the manifest hash changes — running `contAIned init` after editing `manifest.yaml` will detect the change and trigger a rebuild without needing `--rebuild` explicitly.

**Image tagging.** By default, `contAIned init` tags the built image `contained:<workspace-name>` — derived automatically from the directory name. Running `contAIned init` in `~/projects/api-service` produces `contained:api-service`; running it in `~/projects/data-pipeline` produces `contained:data-pipeline`. Both images coexist; neither overwrites the other. To use a fixed name instead, set `runtime.docker.image` in `manifest.yaml` explicitly. The manifest hash and package version are stored as image labels and used to decide whether a rebuild is needed — running `contAIned init` after editing `manifest.yaml` triggers a rebuild automatically.

> **Do not edit hook files directly.** Files under `.contAIned/hooks/` are generated from internal templates and will be overwritten by `contAIned init`. Hook registration, sandbox rules, and permission patterns are managed by `/etc/claude-code/managed-settings.json` baked into the Docker image — they cannot be overridden at runtime. Policy customisation belongs in `manifest.yaml`; structural hook changes should be raised as feature requests.

### Policy schema overview

All governance settings live under the `policy:` key in `manifest.yaml`. The full reference is in [docs/policy-reference.md](./docs/policy-reference.md); examples for common project types are in [docs/examples/](./docs/examples/). The key sections are:

#### `policy.secrets.rules` — secret-file protection

A single ordered list of rules controlling which file paths are treated as secrets. Each rule has `name`, `patterns` (regex list, case-insensitive), `action` (`allow` or `block`), and an optional `reason`. **First match wins** — list `allow` rules before `block` rules to create safe-variant exemptions.

```yaml
policy:
  secrets:
    rules:
      - name: safe-variants
        patterns: ['\.(example|sample|template)']
        action: allow                               # .env.example is always permitted

      - name: dotenv
        patterns: ['(^|[/\\])\.env(\.[^/\\]+)?$']
        reason: "Secret files may not be accessed."
        action: block
```

#### `policy.bash.rules` — Bash command restrictions

An ordered list of rules governing what Bash commands the agent may run. Each rule supports `action: allow` (auto-approve), `action: block` (deny outright), or `action: escalate` (surface an operator prompt). First match wins — list `allow` rules first so safe commands are not caught by later block rules.

```yaml
policy:
  bash:
    rules:
      - name: safe-reads
        patterns: ['^git\s+status\b', '^ls\b', '^cat\b']
        action: allow

      - name: destructive
        patterns: ['^rm\s', '.*\brm\s+-rf\b.*']
        reason: "Destructive deletion is not permitted."
        action: block

      - name: docker-run
        patterns: ['^docker\s+run\b']
        reason: "docker run requires operator approval."
        action: escalate
```

#### `policy.qa.checks` — QA gate

A list of commands run when the agent signals completion. Each entry is either a bare exec-form array (name inferred from `command[0]`) or a named object with an optional `when_changed` glob guard. If `checks` is empty, QA passes trivially.

```yaml
policy:
  qa:
    checks:
      - ["ruff", "check", "."]                    # bare — name inferred as "ruff"

      - name: tests
        command: ["pytest", "tests/", "-x", "-q"]
        when_changed: ["*.py"]                    # skip if no .py files were touched

      - name: go-vet
        command: ["go", "vet", "./..."]
        when_changed: ["*.go"]
```

Commands run with `shell=False` using exec-form arrays. A check whose binary is not installed is skipped automatically. Exit code 5 (no tests collected) is treated as a pass.

---

## Known gaps

### Operator shell escape (`!`) audit coverage

The audit log records every agent tool call. Commands run by the operator directly via `!command` (Claude Code's shell escape) are not agent tool calls and therefore do not pass through the `PreToolUse`/`PostToolUse` hooks.

The `UserPromptSubmit` hook may receive `!` commands — Claude Code's documentation states it fires "when the user submits a prompt, before Claude processes it" with no documented exception for shell escapes. The hook detects and logs any prompt starting with `"!"` as an `OperatorShell` audit event, which will appear in `tracer.db` alongside agent events and be queryable via `/contained:tracer`.

**Caveat:** whether `!` commands actually reach `UserPromptSubmit` is unconfirmed — the SDK may intercept them before the hook fires. If it does fire, coverage is automatic. If not, the fallback is shell history logging (`HISTFILE`, `PROMPT_COMMAND='history -a'`) at the container level.

### QA hook coverage

QA checks are now fully language-agnostic. The `policy.qa.checks` list accepts any exec-form command array — `go vet`, `npx tsc --noEmit`, `cargo clippy`, or any other tool. No Python knowledge is required; see [docs/examples/](./docs/examples/) for ready-to-use manifests for Go, TypeScript, and mixed projects.

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

### Narrative injection budget

The `#review <N>` command retrieves the stored narrative from `tracer.db` and injects it into Claude's context via the `additionalContext` mechanism. Claude Code imposes a size cap on injected context (approximately 10,000 characters). For long sessions — roughly 30 or more turns — the narrative's per-turn closing statements accumulate beyond this budget. The harness silently truncates the injected text, dropping the tail of the narrative. The final conclusion of the session (often the most important part) is typically in the tail.

The stored data in `tracer.db` is complete and unaffected; this is purely a display problem.

**Pending:** implement narrative compaction in a `PreCompact` hook. Claude Code fires `PreCompact` when the session context has grown large enough to warrant compaction — the same signal that indicates the narrative has also grown large. The hook would read the accumulated closings from `tracer.db`, ask Claude to summarize them into a single compact paragraph, and write the result back. This mirrors exactly how Claude Code compacts its own context window: same trigger, same mechanism, bounded output size.

### Egress filtering against a malicious agent

Bash subprocess network access is enforced at the OS level by Claude Code's sandbox (bubblewrap), so it cannot be bypassed by manipulating environment variables. `WebFetch` to non-allowed domains requires operator approval — it will not proceed silently in an unattended session.

The residual gap is outbound connections that bypass both layers: code that opens raw non-HTTP sockets (e.g. `socket.connect` to a hardcoded IP) is not constrained by the sandbox's domain-based rules. Full enforcement of all protocols requires iptables DNAT rules on the Docker bridge that redirect all port traffic through a filtering proxy at the kernel level. Those rules require `CAP_NET_ADMIN` on the host.

**Pending:** investigate whether a lightweight Docker network plugin can install redirect rules at `contAIned init` time, eliminating the need for user-facing `sudo`. Until then, operators who need a hard boundary against this scenario can apply rules manually after init:

```bash
BRIDGE=$(docker network inspect contAIned-net \
  --format '{{index .Options "com.docker.network.bridge.name"}}')
iptables -t nat -A PREROUTING -i "$BRIDGE" -p tcp --dport 80  -j REDIRECT --to-port 3128
iptables -t nat -A PREROUTING -i "$BRIDGE" -p tcp --dport 443 -j REDIRECT --to-port 3128
```

---

## Security model

contAIned's trust boundaries, enforcement architecture, and threat analysis are documented in [docs/security-model.md](./docs/security-model.md). The document covers the principal trust hierarchy, a walkthrough of each containment layer, concrete threat scenarios (prompt injection, adversarial agent, data exfiltration, supply chain attack, operator overreliance), and explicit statements of what contAIned does not guarantee. A cross-reference table maps the analysis to [OWASP Top 10 for LLM Applications](https://owasp.org/www-project-top-10-for-large-language-model-applications/) and [MITRE ATLAS](https://atlas.mitre.org/) for readers working within those frameworks.
