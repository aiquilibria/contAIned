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

```bash
uv add contAIned
# or
pip install contAIned
```

**Prerequisites:** Docker must be installed and running. `cosign` is optional — required only if you enable build provenance (Sigstore) during `contAIned init`. Install cosign: https://docs.sigstore.dev/cosign/system_config/installation/

## Quickstart

```bash
# 1. Go to your project
cd my-project

# 2. Initialize the contAIned workspace (builds the Docker image, wires hooks)
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

All input is forwarded verbatim to the agent. Use the **`/contained:tracer`** skill to query session history, audit logs, file diffs, and the tracer database directly.

---

### `contAIned init [DIRECTORY]`

Scaffolds the contAIned workspace in the target directory (default: current directory).

```bash
contAIned init                         # initialize with interactive wizard
contAIned init ./myrepo                # initialize in a specific directory
contAIned init --force                 # re-run setup wizard (reconfigure model, docker, etc.)
contAIned init --rebuild               # force-rebuild the Docker image without re-running wizard
contAIned init --manifest policy.yaml  # non-interactive: bake a pre-written manifest into the image
```

Runs an interactive wizard to collect Docker and policy settings, then bakes those settings into the Docker image. Pass `--manifest` to skip the wizard and bake a pre-written `manifest.yaml` directly — suitable for CI/CD pipelines or reproducible team setups.

**Policy is enforced at the image layer.** Hook registration and sandbox rules live in `/etc/claude-code/managed-settings.json`, which is copied into the Docker image at build time. Claude Code treats this file as operator-managed policy: hooks registered there cannot be overridden or removed by the agent at runtime. The operator manifest is baked into `/etc/contained/manifest.yaml` inside the image; hooks read policy parameters from that path exclusively.

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

Requires `docker` and `cosign` on the host. Run this before `contAIned` when operating in environments where image integrity matters.

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

**Image tagging — design note.** The built image is always tagged `contained:latest` (or the name configured in `manifest.yaml`). The manifest hash and package version are stored as image labels and used only to decide whether a rebuild is needed. An alternative design would use content-addressed tags (`contained:<hash>`) derived from the manifest and Dockerfile content, making each configuration immutable and allowing multiple manifests to coexist as separate images. The current approach was chosen for simplicity: there is one well-known tag to reference and old images are automatically replaced rather than accumulated. The trade-off is that two sessions with different manifests cannot run simultaneously against distinct images — a rebuild overwrites the shared tag.

> **Do not edit hook files directly.** Files under `.contAIned/hooks/` are generated from internal templates and will be overwritten by `contAIned init`. Hook registration, sandbox rules, and permission patterns are managed by `/etc/claude-code/managed-settings.json` baked into the Docker image — they cannot be overridden at runtime. Policy customisation belongs in `manifest.yaml`; structural hook changes should be raised as feature requests.

---

## Known gaps

### Operator shell escape (`!`) audit coverage

The audit log records every agent tool call. Commands run by the operator directly via `!command` (Claude Code's shell escape) are not agent tool calls and therefore do not pass through the `PreToolUse`/`PostToolUse` hooks.

The `UserPromptSubmit` hook may receive `!` commands — Claude Code's documentation states it fires "when the user submits a prompt, before Claude processes it" with no documented exception for shell escapes. The hook detects and logs any prompt starting with `"!"` as an `OperatorShell` audit event, which will appear in `tracer.db` alongside agent events and be queryable via `/contained:tracer`.

**Caveat:** whether `!` commands actually reach `UserPromptSubmit` is unconfirmed — the SDK may intercept them before the hook fires. If it does fire, coverage is automatic. If not, the fallback is shell history logging (`HISTFILE`, `PROMPT_COMMAND='history -a'`) at the container level.

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
