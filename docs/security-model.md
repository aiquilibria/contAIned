# <span style="color: #64CE91">cont[</span><span style="color: #BD3F39">AI</span><span style="color: #64CE91">]ned</span>
## <span style="color: #64CE91">[</span><span style="color: #BD3F39">✦</span><span style="color: #64CE91">] </span><span style="color: gray">Security and Threat Model</span>

## Contents

- [Introduction](#introduction)
- [Trust Model](#trust-model)
  - [Principals](#principals)
  - [Trust Hierarchy](#trust-hierarchy)
- [Architecture of Containment](#architecture-of-containment)
  - [Image-Layer Policy — The Root of Trust](#image-layer-policy--the-root-of-trust)
  - [Hook Chain](#hook-chain)
  - [Docker Isolation](#docker-isolation)
  - [Egress Proxy](#egress-proxy)
  - [Audit and Tracer](#audit-and-tracer)
- [Threat Scenarios](#threat-scenarios)
  - [Prompt Injection](#prompt-injection)
  - [Jailbroken or Actively Adversarial Agent](#jailbroken-or-actively-adversarial-agent)
  - [Data Exfiltration](#data-exfiltration)
  - [Supply Chain Attack](#supply-chain-attack)
  - [Operator Error and Overreliance](#operator-error-and-overreliance)
- [What contAIned Does Not Guarantee](#what-contained-does-not-guarantee)
- [Framework Cross-Reference](#framework-cross-reference)

---

## Introduction

This document describes the security model, trust boundaries, and threat analysis for contAIned — a coding agent CLI that runs Claude Code inside an isolated Docker container with operator-controlled policy enforcement.

It is intended for operators deploying contAIned in team or CI/CD environments, security reviewers evaluating the tool, and contributors reasoning about what the enforcement layer does and does not cover.

The document is organised around contAIned's own architecture rather than any external checklist. A [framework cross-reference](#framework-cross-reference) at the end maps the analysis to [OWASP Top 10 for LLM Applications](https://owasp.org/www-project-top-10-for-large-language-model-applications/) and [MITRE ATLAS](https://atlas.mitre.org/) for readers who need to satisfy those frameworks specifically.

**What this document covers:**
- The principals contAIned recognises and the trust it places in each
- How the enforcement layer is constructed and why it is tamper-proof
- Concrete threat scenarios and the controls that address each
- Residual risks that are transferred to the operator or not addressed

**What it does not cover:**
- The security of the Claude API itself or Anthropic's infrastructure
- Host OS hardening beyond what Docker provides
- Risks that arise from deploying contAIned in ways not described in the README (e.g. exposing the Docker socket to the agent container)

---

## Trust Model

### Principals

contAIned recognises four principals:

**The operator** is the human who runs `contAIned init`, authors `manifest.yaml`, and approves escalated actions from within a session. The operator is fully trusted. Policy is their instrument — they set it at build time, and it cannot be changed at runtime without their deliberate action (editing the manifest and rebuilding the image).

**The agent** is the Claude Code process running inside the container. The agent is *not* trusted. It may be manipulated by adversarial content in the workspace, may attempt actions that exceed its intended scope, or may — in the limit — actively attempt to subvert enforcement. The entire enforcement architecture is built on the assumption that the agent is an untrusted process whose tool calls must be intercepted and evaluated before execution.

**The workspace** is the project directory bind-mounted into the container at `/workspace`. Its contents are operator-controlled and treated as conditionally trusted: the operator is responsible for the integrity of what they choose to mount. Files the agent writes during a session are tracked in the tracer diff store and require operator review before persisting beyond the session via a git commit.

**External services** are network endpoints the agent may try to reach. They are untrusted by default. The egress proxy allowlist is the operator's explicit declaration of which external services the agent is permitted to contact; everything else is denied.

### Trust Hierarchy

The enforcement layer is ordered by tamper-resistance:

```
┌─────────────────────────────────────────────────────────────┐
│  Rekor transparency log  (optional — Sigstore)              │
│  Append-only public record: image digest ↔ operator OIDC   │
│  identity ↔ timestamp.  Cannot be removed or modified.      │
│  Verified by: contAIned verify (host-side, pre-session).    │
├─────────────────────────────────────────────────────────────┤
│  /etc/claude-code/managed-settings.json  (image layer)      │
│  Hook registrations — highest precedence in Claude Code;    │
│  user-defined hooks are blocked entirely when this is set.  │
│  Protected by Claude Code's own sandbox unconditionally.    │
├─────────────────────────────────────────────────────────────┤
│  /etc/contained/manifest.yaml  (image layer)                │
│  Policy parameters read by hooks at runtime.                │
│  Unreachable by the agent — outside the workspace mount.    │
├─────────────────────────────────────────────────────────────┤
│  .contAIned/hooks/*.py  (workspace, write-protected)        │
│  Hook logic. Writes blocked by restrict_writes.py and by    │
│  the OS-level sandbox denyWrite rule.                       │
├─────────────────────────────────────────────────────────────┤
│  Agent process  (untrusted)                                 │
│  All tool calls pass upward through the layers above        │
│  before executing.                                          │
└─────────────────────────────────────────────────────────────┘
```

The critical property is that the agent cannot reach the layers above it. Hook registration lives in the image, not the workspace. The managed-settings path is blocked by Claude Code's own sandbox independently of any hook — the protection holds even before the first hook runs. There is no circular dependency: the enforcement layer does not rely on the agent to protect itself.

The Rekor layer is optional. When enabled at `contAIned init` time, it extends the trust hierarchy upward: the image itself becomes accountable, not just its enforcement behaviour. See [Build-time provenance](#build-time-provenance-optional) below.

---

## Architecture of Containment

### Image-Layer Policy — The Root of Trust

Policy is baked into the Docker image at build time by `contAIned init`. Two files are written into the image layer — not the workspace — and cannot be modified at runtime:

- `/etc/claude-code/managed-settings.json` — registers all hooks, sandbox rules, permissions, and statusLine. This is the sole settings file active inside the container. Claude Code merges hooks from all settings levels (managed, project, user), so `contAIned init` retires any existing `.claude/settings.json` or `.claude/settings.local.json` by renaming them to dated backups — ensuring managed-settings is the only source of hook registration and no hook fires twice. No CLI flag or runtime configuration can override the managed-settings path itself.
- `/etc/contained/manifest.yaml` — the operator's policy parameters (egress rules, escalation targets, QA settings). Hooks read policy exclusively from this path; there is no fallback to workspace files.

The image is the operator's trust anchor. Changing policy requires editing `manifest.yaml` and running `contAIned init --rebuild` — a deliberate, out-of-band operator action. There is no in-session path to alter enforcement.

### Hook Chain

Every tool call the agent makes passes through a chain of hooks before and after execution:

```
Agent tool call
      │
      ▼
  PreToolUse hooks  ─────────────────────────────────────────────
  restrict_reads.py    Block Read/Glob/Grep outside workspace;
                       block access to secret file patterns
                       (.env, *.pem, id_rsa, credentials.json…)
  restrict_writes.py   Block writes to .contAIned/, .claude/,
                       /etc/claude-code/, and other control-plane paths
  restrict_bash.py     Block dangerous Bash patterns (rm -rf, sudo,
                       curl/wget, raw socket tools); escalate git mutations
      │
      │  (denied calls stop here; allowed calls proceed)
      │
      ▼
  Tool executes
      │
      ▼
  PostToolUse hook  ─────────────────────────────────────────────
  audit.py             Append structured log entry to audit/pipeline.jsonl;
                       record file diffs in tracer.db blob store
      │
      ▼  (on agent Stop)
  Stop hook  ────────────────────────────────────────────────────
  qa.py                Run syntax, lint, type, and test checks;
                       return feedback to agent if checks fail
  summarizer.py        Build narrative + diff summary; mark task closed
```

The three escalation actions available to policy are:
- **block** — deny the call unconditionally; agent receives a clear reason
- **allow** — permit the call without operator involvement
- **escalate** — surface the call to the operator for explicit approval before proceeding

Git mutations (`git commit`, `git push`) default to `escalate`. Package publish operations (`npm publish`, `pip upload`) are blocked unconditionally.

### Docker Isolation

The agent runs inside a Docker container. The host filesystem is not mounted; the workspace is bind-mounted exclusively at `/workspace`. No path-traversal trick can reach files outside this boundary — the kernel enforces it regardless of what the agent attempts.

contAIned also enables Claude Code's built-in `/sandbox` (bubblewrap on Linux), which adds a second enforcement layer at the OS process boundary for Bash subprocesses. The two layers cover different trust boundaries and are complementary:

| Layer | What it covers |
|---|---|
| PreToolUse hooks | Claude Code SDK tool calls (`Write`, `Edit`, `Read`, `Bash`, `WebFetch`) |
| `/sandbox` (bubblewrap) | OS subprocesses spawned by Bash tool calls |

The `managed-settings.json` baked into the image enables the sandbox and adds `denyWrite` rules for `.contAIned/` and `.claude/settings.json` at the OS level — a second enforcement layer behind the hook that already blocks these writes at the SDK level.

### Egress Proxy

An agent session has four distinct outbound channels:

| Channel | Example |
|---|---|
| Claude Code built-in tools | `WebFetch`, `WebSearch` |
| Bash subprocesses | `curl`, `wget`, `nc` |
| Agent-written scripts executed later | Agent writes `exfil.py`, hook sees `python exfil.py` — not the script body |
| MCP / skill processes | Any loaded MCP server making its own HTTP calls |

When `policy.egress.enabled: true`, a filtering proxy sidecar starts alongside the agent on the same Docker network. The agent container receives `HTTP_PROXY` and `HTTPS_PROXY` pointing at the proxy. All outbound HTTP/HTTPS traffic that honours those variables — including `WebFetch`, Bash network tools, and agent-written scripts using standard HTTP libraries — is checked against the operator's domain allowlist. Anything not on the list receives a `403 Forbidden`.

The default allowlist contains only `api.anthropic.com`. Operators add project-specific domains (package registries, documentation hosts) as needed.

**Known limitation.** The proxy relies on `HTTP_PROXY`/`HTTPS_PROXY` environment variables. Code that opens raw sockets directly (e.g. `socket.connect` to a hardcoded IP) bypasses it. Full kernel-level enforcement requires iptables DNAT rules on the Docker bridge; see the [egress documentation](./egress-and-exfiltration-protection.md) for instructions.

### Audit and Tracer

Every tool call is recorded by `audit.py` (PostToolUse) in the tracer database. An optional JSONL mirror is available for operators who need a flat-file export.

**Tracer database** (`tracer.db`) — the primary store. A SQLite database in WAL mode that records every tool call event, task, sub-agent invocation, file diff (content-addressed blob store), and QA result. The `#review <N>` command retrieves the narrative and per-file diff summary for any completed task. The `#db <SQL>` command gives the operator direct SQL access for ad-hoc queries. Writes to `.contAIned/` are blocked by the hook layer, so the database cannot be cleared or tampered with by the agent.

**JSONL mirror** (`audit/pipeline.jsonl`) — an optional append-only flat-file export, enabled by setting `policy.audit.jsonl_export: true` in `manifest.yaml`. Off by default.

Together these provide the forensic record that makes injected or unexpected agent behaviour detectable after the fact, and give the operator the information needed to make an informed decision before approving an escalated git mutation.

---

## Threat Scenarios

### Prompt Injection

**Scenario.** Workspace content the agent reads — source files, commit messages, READMEs, test fixtures — contains adversarial instructions designed to override the agent's intended behaviour. This includes both direct injection (a file the operator asked the agent to read) and indirect injection (a file pulled in transitively, e.g. a dependency's changelog or a fetched web page).

**Primary mitigation: containment of consequences.** contAIned does not attempt to sanitise inputs before passing them to Claude — doing so would break legitimate tasks and is not a reliable defence anyway. Instead, the mitigation is that even a successfully injected instruction can only cause actions the policy permits. The hook layer intercepts every tool call before execution; the blast radius of any injection is bounded by the configured policy regardless of what Claude was told to do.

**Secondary mitigation: tamper-proof enforcement layer.** An injected instruction that tells the agent to "disable the hooks" or "edit settings.json to allow X" will be denied at the tool call layer. Hook registration lives in the image, not in the workspace; writes to `.claude/settings.json` and `.contAIned/` are blocked independently by both the hooks and the OS-level sandbox. There is no path by which a prompt injection can reach the enforcement layer.

**Tertiary mitigations.**
- `CLAUDE.md` establishes operator-authored operating instructions that take precedence over user-turn injections.
- Every tool call is recorded in `tracer.db`, making injected behaviour detectable after the fact.

**Residual risk transferred to operator.** The operator is responsible for not initialising contAIned in a workspace that contains untrusted content they have not reviewed. This mirrors the trust model of running any code in a project directory.

### Jailbroken or Actively Adversarial Agent

**Scenario.** The agent has been successfully jailbroken — either by a prompt injection or by crafted user input — and is now actively attempting to subvert the enforcement layer, exfiltrate data, or take destructive actions.

**What the adversarial agent can do.** It can attempt any tool call that the policy permits. It can write files anywhere inside the workspace that write-hooks allow. It can make outbound requests to allowlisted domains. It can execute Bash commands that are not on the deny list. These are the same capabilities a legitimate agent has — the enforcement layer does not distinguish intent.

**What it cannot do.** It cannot modify the hook scripts, managed-settings, or the manifest — those paths are blocked by both the hook layer and the OS-level sandbox independently. It cannot deregister or replace hooks, because hook registration is in the image layer and Claude Code blocks user-defined hooks entirely when managed settings are active. It cannot reach the host filesystem. It cannot contact non-allowlisted domains (subject to the proxy limitation described above). It cannot approve its own escalated actions — escalation surfaces to the operator's terminal, not back to the agent.

**Residual risk.** A jailbroken agent that stays within policy bounds is indistinguishable from a legitimate agent at the enforcement layer. The tracer audit log and `#review` are the operator's tools for detecting unexpected behaviour after the fact. For high-value workspaces, operators should review `#review` output before approving any escalated git mutation — this is the intended workflow, not an afterthought.

Agent-written code that passes QA checks and executes as part of the task could still contain logic errors or intentionally dangerous behaviour that static analysis does not catch. This risk is bounded by the container and egress boundaries — it cannot escape to the host — but it is not eliminated within the workspace.

### Data Exfiltration

**Scenario.** The agent attempts to send sensitive data — secrets, source code, credentials — outside the workspace, whether deliberately (adversarial) or accidentally (e.g. an injected instruction to "send this file to X").

**Layered controls.**

| Control | What it covers | Strength |
|---|---|---|
| `restrict_reads.py` | Blocks `Read`/`Glob`/`Grep` on secret file patterns (`.env`, `*.pem`, `id_rsa`, …) | Hook-enforced; cannot be disabled at runtime |
| `restrict_bash.py` | Blocks `cat`, `head`, `tail` on the same patterns; blocks `curl`, `wget`, `nc` | Hook-enforced |
| Egress proxy | Filters all outbound HTTP/HTTPS against the domain allowlist | Covers `WebFetch`, Bash network tools, agent-written scripts that honour `HTTP_PROXY` |
| `/sandbox` network | Constrains Bash subprocess network at the OS level | Second layer for Bash-channel exfiltration |

**Residual scenario — indirect reads via agent-written scripts.** The read hooks block direct file access, but an agent could write a Python script that loads `.env` at runtime via `python-dotenv` or `os.environ`, then writes derived values to another workspace file. The hook layer sees `python3 script.py`, not the secret value. The result is captured in `tracer.db` and visible on `#review`. It does not leave the container unless the operator approves a git commit that includes the file.

**Residual scenario — raw socket exfiltration.** An actively adversarial agent could bypass the proxy by opening raw sockets that ignore `HTTP_PROXY`/`HTTPS_PROXY`. Closing this requires kernel-level iptables DNAT rules on the Docker bridge; see [egress documentation](./egress-and-exfiltration-protection.md). The proxy is designed to prevent accidental exfiltration, not to stop a determined adversary.

### Supply Chain Attack

**Scenario.** A malicious actor introduces a vulnerability through a dependency — either at image build time (compromised base image, npm package, Claude Code CLI) or at runtime (agent installs a malicious package during a session).

**Build-time controls.** The contAIned image is built by the operator from a known Dockerfile using packages installed at build time. The image is rebuilt explicitly via `contAIned init --rebuild` — it is never pulled automatically from a registry, so there is no silent auto-update path through which a compromised upstream package could reach a running session without the operator's action. Critically, hook registration and policy parameters are baked into the image — a compromised workspace cannot replace the enforcement layer, because the enforcement layer does not live in the workspace. Dependency pinning and signature verification of base image packages are operator responsibilities.

**Runtime controls.** The agent cannot pull arbitrary packages at runtime without the operator's knowledge:
- `package_publish` operations are blocked unconditionally.
- `git push` and other git mutations require operator escalation.
- The egress proxy allowlist restricts which package registries are reachable — operators who do not need `pypi.org` or `npmjs.com` can omit them from the allowlist entirely.

**Residual risk.** If a registry domain is on the allowlist and the agent installs a malicious package via a Bash command that is not on the deny list, the package executes inside the container. The container boundary limits the blast radius to the workspace. The tracer records the Bash invocation.

### Operator Error and Overreliance

**Scenario.** The operator approves an escalated action — typically a git commit — without reviewing what the agent actually did, accepting incorrect, insecure, or malicious changes into version control.

This is not a technical attack; it is a workflow failure. contAIned addresses it structurally rather than by adding more automated checks.

**Controls.**
- The QA gate (`qa.py`) ensures that syntax, lint, type, and test checks passed before the task was marked closed. The operator is not reviewing raw unvalidated output.
- `#review <N>` shows the agent's full narrative and a per-file diff summary for any completed task before the operator decides whether to approve a commit.
- The tracer blob store retains every version of every file the agent touched, so diffs are always available even after the session ends.
- Escalated git mutations are surfaced to the operator's terminal with an explicit approve/deny prompt — approval requires a deliberate keypress, not just inaction.

**Residual risk.** An operator who approves escalated actions without reading `#review` output accepts the full risk of whatever the agent did. contAIned provides the information needed for an informed decision; it cannot compel the operator to read it.

**Extensibility.** Stricter enforcement — for example, requiring a `#review` step before any escalated action can be approved, or flagging sessions where the operator approved without opening the review — is achievable by building on contAIned's tracer data, which records the full tool call and approval history for every session.

---

## Build-time Provenance (optional)

When the operator enables Sigstore at `contAIned init` time, an additional accountability layer is added **above** the image layer. This does not change the runtime enforcement model — hooks, sandbox, egress proxy, and audit log work identically. What it adds is a non-repudiable record of *who produced the image* and *when*.

### What is recorded

After a successful image build, `contAIned init`:

1. Retrieves the image's SHA256 digest from Docker.
2. Signs that digest as a blob using `cosign sign-blob` (keyless, OIDC-based). This triggers a browser or device-flow authentication with the operator's OIDC provider (GitHub, Google, or any supported issuer).
3. Cosign submits the signature and the short-lived Fulcio certificate (which carries the operator's verified identity) to the Rekor append-only transparency log. The entry cannot be removed or modified.
4. Writes `.contAIned/provenance.yaml` — a local pointer recording the image digest, Rekor log index and entry URL, operator identity, OIDC issuer, and signing timestamp.
5. Writes `.contAIned/provenance.bundle` — the cosign bundle required for offline verification.

### What is verifiable

`contAIned verify` (host-side, run before starting a session) checks:

- The current `contained:latest` image digest matches the digest in `provenance.yaml`. A mismatch means the image was replaced or rebuilt since init — the session is blocked.
- The `provenance.bundle` signature is valid against the recorded identity and issuer via `cosign verify-blob`. This confirms the Rekor entry is intact and the certificate chain is valid.

### Scope and boundaries

**The image is never distributed to a registry.** Sigstore signing here is a build-time provenance record for the local operator, not a supply-chain distribution mechanism. Scenarios where a signed image is shared across machines require a registry-based model and are out of scope.

**Physical access to the machine is out of scope.** Consistent with the existing threat model, attacks requiring host OS compromise or physical device access are not addressed.

**Session-level accountability is not provided.** The signature establishes who built the policy. It does not create a per-session cryptographic record of who approved individual escalations. In the local-only model this is not a gap — the operator who signed the image is the same person running sessions.

**`provenance.yaml` is a local pointer, not the authoritative record.** The Rekor entry is permanent and independently verifiable. `provenance.yaml` is a convenience file used by `contAIned verify`. It is gitignored alongside the rest of `.contAIned/`.

---

## What contAIned Does Not Guarantee

**It does not sanitise inputs.** contAIned does not inspect or filter the content of files the agent reads, prompts the operator sends, or web pages the agent fetches. Prompt injection through workspace content is a real risk; the mitigation is containment of consequences, not prevention of injection.

**It does not enforce kernel-level egress.** The egress proxy covers HTTP/HTTPS traffic that honours `HTTP_PROXY`/`HTTPS_PROXY`. An actively adversarial agent can bypass it with raw socket connections. Full enforcement requires iptables DNAT rules applied with host root access.

**It does not add content-safety filters.** The agent can generate harmful, offensive, or incorrect text. That text ends up as a workspace file, visible on `#review`, and gated behind operator approval before it enters version control — but it is not prevented.

**It does not protect against a compromised operator.** If the operator approves a malicious escalated action, contAIned has no further recourse. The review tooling (`#review`, diff store) is designed to make informed approval easy; it cannot enforce that the operator reads it.

**It does not protect the Claude API or Anthropic infrastructure.** The security of the model itself, the API endpoint, and the TLS connection to `api.anthropic.com` are outside the scope of this model.

**It does not protect against a compromised Docker daemon or host kernel.** contAIned's isolation guarantee is Docker's isolation guarantee. A compromised host can reach inside any container.

---

## Framework Cross-Reference

The table below maps the threat scenarios and controls described in this document to the corresponding entries in [OWASP Top 10 for LLM Applications v1.1](https://owasp.org/www-project-top-10-for-large-language-model-applications/) and [MITRE ATLAS](https://atlas.mitre.org/). It is provided as a lookup aid for readers working within those frameworks; the threat scenarios above are the authoritative analysis.

| Threat / Control | OWASP LLM | MITRE ATLAS |
|---|---|---|
| Prompt injection — containment of consequences | [LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) | [AML.T0051](https://atlas.mitre.org/techniques/AML.T0051), [AML.T0054](https://atlas.mitre.org/techniques/AML.T0054) |
| Output injection — bash restrictions, QA gate, container boundary | [LLM02](https://genai.owasp.org/llmrisk/llm02-insecure-output-handling/) | [AML.T0051.002](https://atlas.mitre.org/techniques/AML.T0051/002) |
| Excessive agency — layered hook controls, mutation escalation, publish blocking | [LLM08](https://genai.owasp.org/llmrisk/llm08-excessive-agency/) | [AML.T0061](https://atlas.mitre.org/techniques/AML.T0061) |
| Sensitive information disclosure — read hooks, bash restrictions, egress proxy | [LLM06](https://genai.owasp.org/llmrisk/llm06-sensitive-information-disclosure/) | [AML.T0051](https://atlas.mitre.org/techniques/AML.T0051), [AML.T0024](https://atlas.mitre.org/techniques/AML.T0024) |
| Supply chain — image-layer policy, egress allowlist, publish blocking | [LLM05](https://genai.owasp.org/llmrisk/llm05-supply-chain-vulnerabilities/) | [AML.T0010](https://atlas.mitre.org/techniques/AML.T0010) |
| Overreliance — QA gate, `#review`, diff store, escalation workflow | [LLM09](https://genai.owasp.org/llmrisk/llm09-overreliance/) | — |
| Training data integrity — diff store, full write traceability | [LLM03](https://genai.owasp.org/llmrisk/llm03-training-data-poisoning/) | [AML.T0020](https://atlas.mitre.org/techniques/AML.T0020) |
| Insecure plugin design — all tools intercepted by image-layer hooks | [LLM07](https://genai.owasp.org/llmrisk/llm07-insecure-plugin-design/) | — |
| Model theft — read hooks, egress proxy (no weights on-premises) | [LLM10](https://genai.owasp.org/llmrisk/llm10-model-theft/) | [AML.T0024](https://atlas.mitre.org/techniques/AML.T0024) |
| Denial of service — Docker resource limits; per-developer deployment model | [LLM04](https://genai.owasp.org/llmrisk/llm04-model-denial-of-service/) | [AML.T0029](https://atlas.mitre.org/techniques/AML.T0029) |
| RAG / vector DB poisoning | — | [AML.T0060](https://atlas.mitre.org/techniques/AML.T0060) — **not applicable** (no retrieval layer) |
| Adversarial content / societal harm | — | [AML.T0043](https://atlas.mitre.org/techniques/AML.T0043) — **no technical prevention**; operator review is the control |
