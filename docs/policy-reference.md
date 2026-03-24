# Policy Reference

Full annotated reference for `.contAIned/manifest.yaml`.

The manifest has three top-level sections: `runtime`, `agent`, and `policy`.
This document covers `policy` in full. For a quick orientation see the
[Customizing policy](../README.md#customizing-policy) section of the README.

---

## Providing a manifest to `contained init`

`contained init` requires a manifest via one of two flags:

| Flag | Description |
|---|---|
| `--manifest <path>` | Local `manifest.yaml` file |
| `--mainlined <url>` | Mainlined policy URL — fetched, validated, and written to `.contAIned/manifest.yaml` before scaffolding |

Running without either flag prints a starter manifest to stderr and exits non-zero.

**`--mainlined` flow:**

1. Fetches the manifest YAML from the URL (HTTP GET). Set `MAINLINED_TOKEN` in the environment for private policies.
2. Validates the fetched YAML against the manifest schema.
3. Writes it to `.contAIned/manifest.yaml`.
4. Records the URL in the manifest under `policy.mainlined.url` so that future sessions can detect drift if the remote policy is updated.
5. Proceeds with normal scaffolding as if `--manifest` had been passed.

---

## Rule evaluation — first match wins

Both `secrets.rules` and `bash.rules` use **first-match-wins** ordering.
Rules are evaluated from top to bottom; the first rule whose `patterns` match
the input determines the outcome. List `action: allow` rules before
`action: block` rules to create exemptions for safe variants.

---

## `policy.secrets`

Controls which file paths are treated as secrets. Applied by the
`restrict_reads.py`, `restrict_writes.py`, and `restrict_bash.py` hooks.

### `policy.secrets.rules`

Type: `list[Rule]` | Default: `[]` (no secret-file checks)

An ordered list of rules. Each rule:

| Field | Type | Required | Description |
|---|---|:---:|---|
| `name` | string | ✓ | Human-readable identifier shown in denial messages |
| `patterns` | list[string] | ✓ | Regex patterns matched against the file path (re.IGNORECASE) |
| `action` | `allow` \| `block` | ✓ | `allow` — permit; `block` — deny with `reason` |
| `reason` | string | | Message shown to the agent when the rule blocks an operation |

**Evaluation:** for each file access, rules are checked in order. The first
rule whose patterns match the path (via `re.search`) determines the outcome.
If no rule matches, the operation is permitted.

**Example:**

```yaml
policy:
  secrets:
    rules:
      # allow rules first — safe variants are never blocked
      - name: safe-variants
        patterns: ['\.(example|sample|template)']
        action: allow

      # block rules after — checked only if no allow rule matched
      - name: dotenv
        patterns: ['(^|[/\\])\.env(\.[^/\\]+)?$']
        reason: "Secret files (credentials, keys, .env) may not be accessed."
        action: block

      - name: keys
        patterns: ['\.(pem|key|p12|pfx|jks|keystore)$']
        reason: "Secret files (credentials, keys, .env) may not be accessed."
        action: block

      - name: ssh-keys
        patterns: ['(^|[/\\])id_(rsa|dsa|ecdsa|ed25519)$']
        reason: "Secret files (credentials, keys, .env) may not be accessed."
        action: block

      - name: credential-files
        patterns: ['(^|[/\\])(credentials|secrets|service_account)\.(json|yaml|yml)$']
        reason: "Secret files (credentials, keys, .env) may not be accessed."
        action: block

      - name: secret-extension
        patterns: ['(^|[/\\])\.secret(s)?$']
        reason: "Secret files (credentials, keys, .env) may not be accessed."
        action: block
```

---

## `policy.bash`

Controls which Bash commands the agent may run. Applied by
`restrict_bash.py`.

### `policy.bash.rules`

Type: `list[Rule]` | Default: `[]` (all commands escalate to operator)

An ordered list of rules. Each rule:

| Field | Type | Required | Description |
|---|---|:---:|---|
| `name` | string | ✓ | Human-readable identifier |
| `patterns` | list[string] | ✓ | Regex patterns matched against the full command string |
| `action` | `allow` \| `block` \| `escalate` | ✓ | See below |
| `reason` | string | | Message shown to the agent when the rule blocks a command |

**Actions:**

| Action | Effect |
|---|---|
| `allow` | Auto-approve without prompting the operator |
| `block` | Deny outright; agent receives the `reason` string |
| `escalate` | Surface an operator approval prompt; logged as an exception |

**Fallback:** if no rule matches, the command escalates to the operator
(`permissionDecision: ask`).

**Example:**

```yaml
policy:
  bash:
    rules:
      # allow rules first — safe read-only commands need no operator prompt
      - name: safe-git-reads
        patterns:
          - '^git\s+status\b'
          - '^git\s+log\b'
          - '^git\s+diff\b'
          - '^git\s+show\b'
          - '^git\s+branch\b'
          - '^git\s+stash\s+list\b'
          - '^git\s+remote\b'
        action: allow

      - name: safe-read-only
        patterns: ['^ls\b', '^pwd\b', '^echo\s', '^which\s', '^grep\b', '^rg\b', '^find\b', '^cat\b', '^wc\b']
        action: allow

      # block rules — deny outright with a clear reason
      - name: destructive
        patterns: ['^rm\s', '.*\brm\s+-rf\b.*']
        reason: "Destructive file deletion is not permitted."
        action: block

      - name: privilege_escalation
        patterns: ['^sudo\s']
        reason: "Privilege escalation is not permitted."
        action: block

      - name: network_exfiltration
        patterns: ['^(curl|wget|nc|ncat)\s']
        reason: "Outbound network calls from Bash are not permitted."
        action: block

      # escalate — operator sees a prompt and decides
      - name: docker-run
        patterns: ['^docker\s+run\b']
        reason: "docker run requires explicit operator approval."
        action: escalate
```

---

## `policy.qa`

Configures the QA gate that runs when the agent signals completion (Stop hook).
If any check fails, the agent receives feedback and must fix the issues before
stopping.

### `policy.qa.checks`

Type: `list[Check]` | Default: `[]` (QA passes trivially)

Each entry is either:

**Bare exec-form array** — name inferred from `command[0]`:
```yaml
- ["ruff", "check", "."]
- ["go", "vet", "./..."]
```

**Named object**:

| Field | Type | Required | Description |
|---|---|:---:|---|
| `name` | string | | Display name (defaults to `command[0]`) |
| `command` | list[string] | ✓ | Exec-form command array; runs with `shell=False` |
| `when_changed` | list[string] | | fnmatch glob patterns; check is skipped if no touched file matches |

**`when_changed`** uses `fnmatch.fnmatch` against the basenames of files
touched during the session (from the tracer). Use it to skip slow checks when
irrelevant files were modified.

**Special cases:**
- A check whose binary is not installed is **skipped** (not failed).
- Exit code `5` (pytest "no tests collected") is treated as **pass**.

**Example:**

```yaml
policy:
  qa:
    checks:
      - name: lint
        command: ["ruff", "check", "."]
        when_changed: ["*.py"]

      - name: format
        command: ["ruff", "format", "--check", "."]
        when_changed: ["*.py"]

      - name: types
        command: ["pyright"]
        when_changed: ["*.py"]

      - name: tests
        command: ["pytest", "tests/", "-x", "--tb=short", "-q"]
        when_changed: ["*.py"]

      - name: go-vet
        command: ["go", "vet", "./..."]
        when_changed: ["*.go"]

      - name: ts-build
        command: ["npx", "tsc", "--noEmit"]
        when_changed: ["*.ts", "*.tsx"]
```

---

## `policy.network`

Controls outbound network access for both Claude Code tools and Bash subprocesses.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable network filtering |
| `allowed_domains` | list[string] | `[api.anthropic.com, code.claude.com, docs.anthropic.com]` | Domains silently permitted for WebFetch/WebSearch; all others require operator approval |

`api.anthropic.com` must remain in `allowed_domains` — the agent cannot function without it.

---

## `policy.audit`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Always-on audit log in `tracer.db`; cannot be disabled |
| `jsonl_export` | bool | `false` | Mirror audit entries to `.contAIned/audit/pipeline.jsonl` |

---

## `policy.sigstore`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Sign the Docker image with Sigstore (cosign) during `contAIned init` |
| `rekor_url` | string | `https://rekor.sigstore.dev` | Rekor transparency log endpoint |
| `fulcio_url` | string | `https://fulcio.sigstore.dev` | Fulcio certificate authority endpoint |

Requires `cosign` on the host. When disabled, `provenance_log` is written as an empty list in `tracer.db`.

---

## `policy.mcp`

| Field | Type | Default | Description |
|---|---|---|---|
| `approved_servers` | list[string] | `[]` | MCP server names whose tools are auto-approved |

Unlisted servers surface an operator prompt and are logged as exceptions.

---

## `policy.skills`

| Field | Type | Default | Description |
|---|---|---|---|
| `approved_skills` | list[string] | `[]` | Skill names that are auto-approved |

Unlisted skills surface an operator prompt and are logged as exceptions.

---

## `policy.mainlined`

Coordinates with a central `m<AI>nlined` policy store. When `url` is set,
`contAIned init` pulls the resolved policy for the configured `policy_name`
and bakes it into the manifest. `policy_ref` and `policy_version` are written
automatically by `policy pull` and must not be edited manually.

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | `""` | `m<AI>nlined` base URL; empty = offline / local-only mode |
| `policy_name` | string | `""` | Human-readable policy identity used in `m<AI>nlined` (e.g. `acme-corp-python`) |
| `policy_ref` | string | `""` | SHA-256 of the policy at pull time; written by `policy pull` |
| `policy_version` | string | `""` | Semantic version from `m<AI>nlined`; written by `policy pull` |

`policy_ref` and `policy_version` are cargo-carried into `invocation_hash` via
`manifest_hash`, so every proof is cryptographically bound to the exact central
policy version that was active when the image was built. When `url` is empty,
these fields remain blank and only the local manifest content is hashed.

---

## Defaults

If the manifest is unreadable (missing file, parse error), all rule lists default
to `[]`. This means:

- `secrets.rules: []` → no secret-file checks applied
- `bash.rules: []` → all commands escalate to the operator
- `qa.checks: []` → QA passes trivially

Operators who want enforcement must put rules in their manifest. There are no
hidden built-ins.
