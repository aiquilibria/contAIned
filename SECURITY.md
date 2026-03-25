# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest  | ✓         |

We support the latest released version. Security fixes are not backported to older releases.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

To report a vulnerability, open a [GitHub Security Advisory](https://github.com/lab-v2/contAIned/security/advisories/new) in this repository. This keeps the report private while we triage and prepare a fix.

Include as much of the following as you can:

- Description of the vulnerability and its potential impact
- Steps to reproduce (proof-of-concept, command sequences, or sample manifest)
- Affected component (CLI, hook scripts, Dockerfile, Sigstore integration, egress filtering)
- Any suggested mitigations you have identified

## Response Process

1. **Acknowledgement** — we will acknowledge receipt within 3 business days.
2. **Assessment** — we will assess severity and scope within 7 business days and reply with our findings.
3. **Fix** — critical and high-severity issues will be prioritised for the next release. We will coordinate disclosure timing with you.
4. **Disclosure** — once a fix is released, we will publish a GitHub Security Advisory crediting the reporter (unless you prefer to remain anonymous).

## Scope

This policy covers:

- The `contAIned` CLI binary and its build pipeline
- The Docker image and its enforcement hooks (`restrict_reads.py`, `restrict_writes.py`, `restrict_bash.py`, `audit.py`, `qa.py`)
- The Sigstore signing and verification flow
- The egress filtering mechanism

**Out of scope:**

- The security of the Claude API or Anthropic's infrastructure
- Vulnerabilities in upstream dependencies (Docker, Claude Code, Sigstore) — please report those to the respective upstream projects
- Issues that require the attacker to already have operator-level access to the host system

## Security Design

contAIned's security model, trust boundaries, and threat analysis are documented in [docs/security-model.md](docs/security-model.md). Known residual risks are documented in the [Known gaps](README.md#known-gaps) section of the README.
