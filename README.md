# <span style="color: #64CE91">cont[</span><span style="color: #BD3F39">AI✦</span><span style="color: #64CE91">]ned</span>
## take back control of your agent!

A coding agent CLI built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview).

The agent operates within a defined workspace inside an isolated Docker container. All tool calls are audited. Policy is baked into the container image at build time and enforced by hook scripts before every tool call — the agent cannot change the rules it operates under. High-risk actions (such as git mutations) can be configured to escalate to the operator for explicit approval rather than being allowed or blocked outright.

## Contents

- [Why contAIned?](#why-contained)
- [Install](#install)
- [Quickstart](#quickstart)
- [Commands](#commands)
  - [`contAIned`](#contained-1)
  - [`contAIned init`](#contained-init-directory)
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
- [OWASP Top 10 for LLM Applications](#owasp-top-10-for-llm-applications)

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
| Egress filtering — outbound network allowlist | ✗ | ✗ | ◑ (proxy sidecar; prevents accidental exfiltration) |
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

**Hash commands** (handled by the `UserPromptSubmit` hook before the agent sees them):

| Command | What it does |
|---|---|
| `#review` | List recent completed tasks |
| `#review <N>` | Show narrative + diff summary for task N |
| `#db` | Query `tracer.db` — last 10 tasks |
| `#db <SQL>` | Run arbitrary SQL against `tracer.db` |
| `#status` | Show last 20 audit-log entries |
| `#policy` | Show the effective policy manifest (read-only) |

Any other input is forwarded verbatim to the agent.

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

An agent session has multiple channels for sending data out of the workspace: Claude Code's built-in `WebFetch` tool, Bash subprocesses (`curl`, `wget`), and scripts the agent writes and then executes. Different controls cover different channels — none of the simpler approaches cover all three.

contAIned addresses this with a filtering proxy sidecar. When `policy.egress.enabled` is `true` in `manifest.yaml`, a second container running `contained.proxy` starts alongside the agent on the same Docker network. The agent container receives `HTTP_PROXY` and `HTTPS_PROXY` pointing at the proxy. All outbound HTTP and HTTPS traffic — including `WebFetch`, Bash network tools, and agent-written scripts that honor the proxy environment variables — is checked against a domain allowlist. Anything not on the list gets a `403 Forbidden`.

Enable it and set the allowlist in `.contAIned/manifest.yaml`, then rebuild the image:

```yaml
policy:
  egress:
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

The proxy sidecar starts and stops automatically with each session.

**Design intent — accidental exfiltration.** The proxy works by injecting `HTTP_PROXY` and `HTTPS_PROXY` into the agent container. This covers the common cases: `WebFetch` calls, Bash network tools, and agent-written scripts that use standard HTTP libraries. It is designed to prevent the agent from accidentally sending data outside the workspace — not to stop an agent that is actively trying to circumvent it. A malicious agent could bypass the proxy by opening raw sockets that ignore the env vars. Addressing that requires kernel-level enforcement; see [Known gaps](#known-gaps) below.

### Tracer

`tracer.db` (SQLite, WAL mode) records every task, sub-agent invocation, file diff (content-addressed blob store), and QA result. The `#db` hash command gives the operator direct SQL access.

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

The egress proxy relies on `HTTP_PROXY` / `HTTPS_PROXY` environment variables, which are visible to the agent. A cooperative or accidentally misbehaving agent is contained; an agent actively trying to exfiltrate data can bypass the proxy by dialing sockets directly.

Full enforcement requires iptables DNAT rules on the Docker bridge that transparently redirect all outbound traffic through the proxy at the kernel level — making bypass impossible regardless of how the agent opens connections. The rules require `CAP_NET_ADMIN` on the host, which means root or `sudo`.

`contAIned init` runs as an unprivileged user and cannot write iptables rules directly. Docker's own daemon solves an analogous problem — it installs bridge rules for every network it creates — but it does so because it runs as root. There is currently no way to delegate the iptables write to the Docker daemon without a custom network driver plugin, which is significant additional scope.

**Pending:** investigate whether a lightweight Docker network plugin can install the redirect rules at `contAIned init` time via the daemon, eliminating the need for user-facing `sudo`. Until then, operators who need a hard boundary against a malicious agent can apply the rules manually after init:

```bash
BRIDGE=$(docker network inspect contAIned-net \
  --format '{{index .Options "com.docker.network.bridge.name"}}')
iptables -t nat -A PREROUTING -i "$BRIDGE" -p tcp --dport 80  -j REDIRECT --to-port 3128
iptables -t nat -A PREROUTING -i "$BRIDGE" -p tcp --dport 443 -j REDIRECT --to-port 3128
```

---

## OWASP Top 10 for LLM Applications

Analysis of the [OWASP Top 10 for LLM Applications v1.1](https://owasp.org/www-project-top-10-for-large-language-model-applications/) as applied to contAIned. Each item notes whether it is applicable, why, and how the risk is addressed (directly, transferred to the operator, or not materially relevant).

| # | Risk | Applicable | Disposition |
|---|------|:----------:|-------------|
| LLM01 | Prompt Injection | Yes | Hooks bound the blast radius of any injected instruction to what policy permits; workspace contents are operator-trusted, consistent with Claude Code's own trust model |
| LLM02 | Insecure Output Handling | Yes | QA gate, bash restrictions, and Docker container boundaries collectively limit the damage any agent-written code can cause before or after execution |
| LLM03 | Training Data Poisoning | Potential | contAIned performs no model training; if the workspace contains training datasets, their integrity is the operator's responsibility — any agent modifications are fully traceable via the diff store |
| LLM04 | Model Denial of Service | No | contAIned is a per-developer tool with no shared capacity or external endpoint; Docker resource limits cap per-session consumption |
| LLM05 | Supply Chain Vulnerabilities | Potential | The contAIned image is hardened at build time; workspace contents are operator-trusted; runtime package installation is gated by egress policy, preventing the agent from pulling arbitrary dependencies |
| LLM06 | Sensitive Information Disclosure | Yes | Read hooks block direct access to secret files; egress proxy prevents network exfiltration; indirect writes via agent-written scripts are captured in the tracer blob store and detectable on review |
| LLM07 | Insecure Plugin Design | Potential | No external plugin API exists; Claude Code agent skills are available via operator escalation — manifest policy (`escalate` / `block`) governs which tool calls surface to the operator and which are denied outright |
| LLM08 | Excessive Agency | Yes | The primary design target — layered controls span filesystem scope, per-tool-call policy hooks, destructive action blocking, egress filtering, and operator escalation for mutations |
| LLM09 | Overreliance | Yes | `#review` and the diff store make informed operator escalation straightforward; stricter enforcement is achievable by integrating approval workflows with contAIned's tracer data |
| LLM10 | Model Theft | Potential | Claude runs as a hosted API with no weights on-premises; if the workspace contains a deployable model artifact, the same egress proxy and read-hook controls that protect other sensitive data apply |

---

### LLM01 — Prompt Injection

**Applicable.** Workspace content the agent reads — source files, commit messages, READMEs — can contain adversarial instructions designed to override the agent's intended behavior.

contAIned does not sanitize inputs before passing them to Claude; it is not in a position to do so without breaking legitimate tasks. Instead, the mitigation is **containment of consequences**: even a successfully injected instruction can only cause actions that the policy permits. The hook layer (PreToolUse) intercepts every tool call before execution, so the blast radius of any injection is bounded by the configured policy regardless of what Claude was told to do.

Secondary mitigations:
- `CLAUDE.md` operating instructions establish a trusted system prompt that takes precedence over user-turn injections.
- The audit log and tracer DB record every tool call, making injected behavior detectable after the fact.
- Workspace content is operator-controlled: a workspace initialized by the operator is treated as trusted, consistent with how Claude Code itself treats its working directory.

**Residual risk transferred to operator:** the operator is responsible for not initializing contAIned in a workspace that contains untrusted content they have not reviewed. This mirrors the trust model of running any code in a project.

---

### LLM02 — Insecure Output Handling

**Applicable.** Claude writes code that runs inside the container. If that code contains vulnerabilities or malicious logic, executing it could cause damage even within the sandbox boundary.

Direct mitigations:
- The QA hook (`qa.py`) runs syntax, lint, type, and test checks before the agent can declare a task complete. Code with obvious defects is rejected and the agent receives feedback.
- `restrict_bash.py` blocks or escalates dangerous Bash patterns (destructive deletion, privilege escalation, raw network calls) before they execute.
- The Docker container limits the execution environment: agent-written code that runs inside the container cannot reach the host filesystem, cannot escalate privileges, and cannot make arbitrary outbound network calls.

**Residual risk:** agent-written code that passes QA and runs as part of the task could still have logic errors or intentionally dangerous behavior that static checks do not catch. This risk is bounded by the container and egress boundaries; it does not escape to the host.

---

### LLM03 — Training Data Poisoning

**Potentially applicable.** contAIned uses Claude as an inference-time API and performs no model training. If the workspace does not contain training datasets, this risk does not apply.

If training data is present in the workspace, its initial integrity is the operator's responsibility — contAIned treats workspace contents as trusted, consistent with its overall trust model. Any modifications the agent makes to those files during a session are fully traceable: the tracer diff store captures a content-addressed snapshot of every write, and `#review` surfaces per-file diffs for operator inspection before changes are committed.

---

### LLM04 — Model Denial of Service

**Not applicable.** contAIned is a per-developer tool, not a multi-tenant service with shared capacity. There is no externally exposed endpoint and no shared resource pool that an attacker could exhaust.

Within a single session, resource consumption is bounded by Docker resource limits configured in `manifest.yaml` (`memory`, `cpus`). No token-budget or request-rate enforcement exists at the application layer — this is accepted given the deployment model.

---

### LLM05 — Supply Chain Vulnerabilities

**Potentially applicable.** Risk is addressed across three distinct layers:

**Image build — hardened by contAIned.** The contAIned Docker image installs third-party software at build time (`nodejs`, `npm`, `@anthropic-ai/sandbox-runtime`, the Claude Code CLI). The image is built by the operator from a known Dockerfile, pinning and signature verification can be applied at the operator's discretion, and the image is rebuilt explicitly (`contAIned init --rebuild`) rather than pulled from a registry.

**Workspace contents — operator-trusted.** The workspace is initialized by the operator and treated as trusted. Introducing a malicious dependency into the workspace is equivalent to doing so in any codebase — it is an operator responsibility, not a contAIned control-plane concern.

**Runtime installation — gated by egress policy.** The agent cannot pull arbitrary packages at runtime: `package_publish` is blocked unconditionally, `git_mutations` (including `git push`) require operator escalation, and the egress proxy allowlist restricts which package registries are reachable during a session.

---

### LLM06 — Sensitive Information Disclosure

**Applicable.** Secrets (API keys, credentials, private keys) may be present in the workspace or injected as environment variables.

Direct mitigations:
- `restrict_reads.py` blocks the `Read` tool from accessing files matching secret patterns (`.env`, `*.pem`, `id_rsa`, `credentials.json`, etc.), with explicit allowance for `.example`/`.sample` variants.
- `restrict_bash.py` blocks Bash read commands (`cat`, `head`, `tail`, etc.) targeting the same file patterns.
- Egress filtering constrains where data can be sent: even if a secret reached the agent's context, it cannot be exfiltrated via `WebFetch`, Bash network tools, or agent-written scripts that honor the proxy environment variables — only `api.anthropic.com` is reachable by default.

**Residual scenario — indirect reads via agent-written scripts:** the `Read` and Bash hooks block direct reads, but an agent could write a Python script that loads the `.env` file at runtime (`python-dotenv`, `os.environ`) and then writes derived values to another file. The hook layer sees a `python3 script.py` invocation, not the secret value. This scenario results in the secret being written to a workspace file — it is captured in the tracer blob store and visible on `#review`, making it forensically detectable. It does not escape the container unless the operator approves a `git push` that includes the file.

**Transferred to operator:** git mutation escalation means a human must approve any commit. An operator who reviews diffs before approving (which `#review` is designed to support) would catch a secret written to a workspace file before it enters version control.

---

### LLM07 — Insecure Plugin Design

**Potentially applicable.** contAIned does not expose an external plugin API. The agent's tools are Claude Code's built-in SDK tools (`Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebFetch`, `Agent`), all of which are intercepted by PreToolUse hooks before execution.

Claude Code agent skills — which extend the agent's capabilities — are available but governed by the manifest policy. The `escalate` action surfaces any tool call matching a skill invocation to the operator for explicit approval before it proceeds; the `block` action prevents it outright. No skill or tool call can execute without first passing through the hook layer.

---

### LLM08 — Excessive Agency

**Applicable — and the primary design target of contAIned.**

The core thesis of contAIned is that an LLM agent should not be able to take arbitrary actions in the world without human oversight. Mitigations are layered across the stack:

| Layer | Mechanism |
|---|---|
| Filesystem scope | Docker bind-mount limits agent to `/workspace`; host is invisible |
| Tool call interception | PreToolUse hooks evaluate every `Write`, `Edit`, `Read`, `Bash` call before execution |
| Destructive action blocking | `rm`, `sudo`, raw network calls blocked by policy |
| Mutation escalation | `git_mutations: escalate` surfaces commits and pushes to operator for approval |
| Publish blocking | `npm publish`, `pip upload`, `twine upload` blocked unconditionally |
| Outbound network | Egress proxy allowlist — only `api.anthropic.com` reachable by default |
| Audit trail | Every tool call logged; content-addressed diffs stored for review |
| QA gate | Agent cannot declare completion until quality checks pass |
| Operator escalation | `#review` gives operator full narrative + diff; escalated actions require explicit approval |

The escalation model is explicit: policy distinguishes `block` (never permitted), `allow` (always permitted), and `escalate` (deferred to operator). Git mutations default to `escalate` precisely because they are the primary mechanism by which agent work persists beyond the session.

---

### LLM09 — Overreliance

**Applicable.** contAIned makes informed operator escalation the default, not an afterthought:
- `#review <N>` shows the agent's full narrative and a per-file diff summary for any completed task.
- The tracer blob store retains every version of every file the agent touched, so diffs are always available.
- The QA gate ensures that syntax, lint, and type checks passed before the task closed.
- Git mutations require explicit operator escalation — the operator sees the diff before any change is committed.

Stricter enforcement — for example, requiring a review step before any escalated action is approved, or flagging tasks where the operator approved without opening `#review` — is achievable by building on contAIned's tracer data, which records the full tool call and approval history for every session.

---

### LLM10 — Model Theft

**Potentially applicable.** Claude runs as a hosted API; no model weights are present in the contAIned environment and there is nothing to exfiltrate in the standard case.

If the workspace contains a deployable model artifact (weights, checkpoints, exported files), the same controls that protect other sensitive data apply: read hooks restrict direct access to sensitive files, and the egress proxy prevents the agent from transmitting data outside the allowlist. Any agent access to model artifacts is captured in the tracer audit log and visible on `#review`.
