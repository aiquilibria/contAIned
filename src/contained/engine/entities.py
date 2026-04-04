"""Entity model for the Cedar-inspired policy engine.

All entity types are frozen Pydantic BaseModels — immutable after construction,
JSON-serialisable via model_dump(), and self-describing via model_fields (used
by the validator). ValidationError is raised at the builder boundary if input
is malformed; the engine itself never receives an invalid entity.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

# ---------------------------------------------------------------------------
# Outcome and core result types
# ---------------------------------------------------------------------------


class Outcome(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"
    DEFER = "defer"  # No rule matched — hand off to Claude Code's pipeline.


class Decision(BaseModel):
    """Result of a single engine evaluation."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    rule_id: str | None = None  # None only when outcome is DEFER.
    reason: str | None = None  # Populated for DENY and ESCALATE.

    @property
    def rule_version(self) -> str | None:
        """Extract the vN: prefix from rule_id, e.g. 'v0' from 'v0:secrets:dotenv'."""
        if self.rule_id and ":" in self.rule_id:
            prefix = self.rule_id.split(":")[0]
            if re.match(r"^v\d+$", prefix) or prefix == "builtin":
                return prefix
        return None


# ---------------------------------------------------------------------------
# Rule (parsed from manifest)
# ---------------------------------------------------------------------------


class Rule(BaseModel):
    """A single policy rule parsed from the manifest.

    Enforcement rules (effect: permit / forbid / escalate) require action and
    resource_type and are evaluated against every matching tool call.

    Classifier rules (effect: define) declare computed attributes (e.g.
    is_secret, is_safe_variant) via a ``define`` block containing attribute
    names mapped to ``{patterns: [...]}`` dicts.  They are never evaluated as
    enforcement rules; the entity builder reads them to pre-compute resource
    attributes before enforcement begins.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    effect: Literal["permit", "forbid", "escalate", "define"]
    action: list[str] = []
    resource_type: str = "*"
    when: list[str] = []
    unless: list[str] = []
    reason: str | None = None
    tags: list[str] = []
    define: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Principal entity
# ---------------------------------------------------------------------------


class AgentSession(BaseModel):
    """The principal — the running agent session."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    ecosystem: Literal["go", "python", "node", "typescript"] | None = None
    task_state: Literal["open", "closed"] = "open"
    task_phase: Literal["initialization", "active", "qa", "review"] = "active"
    tool_call_count: int = 0


# ---------------------------------------------------------------------------
# Resource entities
# ---------------------------------------------------------------------------


class FilePath(BaseModel):
    """Resource entity for Read, Write, Edit, MultiEdit, and Grep tool calls."""

    model_config = ConfigDict(frozen=True)

    raw_path: str
    normalized: str
    in_workspace: bool
    in_tmp: bool
    is_secret: bool
    is_safe_variant: bool
    in_control_plane: bool
    extension: str | None
    relative_path: str

    @model_validator(mode="after")
    def safe_variant_requires_secret(self) -> "FilePath":
        if self.is_safe_variant and not self.is_secret:
            raise ValueError(
                "is_safe_variant=True requires is_secret=True "
                "(a safe variant is a safe form of a secret file)"
            )
        return self


class GlobPattern(BaseModel):
    """Resource entity for Glob tool calls.

    Attributes:
        pattern:      Raw glob pattern as passed to the Glob tool.
        prefix_path:  Longest directory prefix before the first wildcard character
                      (*, ?, [). None when the pattern has no literal directory
                      prefix (e.g. **/*.py).
        in_workspace: True when prefix_path is within /workspace, or when the
                      pattern is workspace-relative (no leading /).
    """

    model_config = ConfigDict(frozen=True)

    pattern: str
    prefix_path: str | None
    in_workspace: bool


# Glob wildcard characters — used by build_glob_pattern_entity().
_WILDCARD_RE = re.compile(r"[*?\[\]]")

# Shell operators that make a bash command compound.
_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|"})


class BashCommand(BaseModel):
    """Resource entity for Bash tool calls.

    Token decomposition (all derived via shlex.split):
      verb            — first token, e.g. "git"
      subcommand      — second token if it doesn't start with "-", e.g. "push"
      args            — all flag tokens (starting with "-"), e.g. ["--force", "--dry-run"]
      positional_args — non-flag tokens after verb+subcommand, e.g. ["origin", "main"]
      target_path     — first token in positional_args that looks like a filesystem path
      target_is_secret— True if target_path matches a secret file pattern
      is_compound     — True if a shell operator (&&, ||, ;, |) appears as a top-level token

    Note: kwargs (flag→value pairs like --mainlined http://...) are not modelled in
    Phase 1 — the condition DSL lacks subscript notation for dict access. Add in Phase 2.
    """

    model_config = ConfigDict(frozen=True)

    raw: str
    verb: str
    subcommand: str | None
    args: list[str]  # flag tokens, e.g. ["--force", "-v"]
    positional_args: list[str]  # non-flag tokens after verb+subcommand
    target_path: str | None  # first path-looking token in positional_args
    target_is_secret: bool
    target_in_workspace: bool  # True if target_path is within /workspace
    target_in_tmp: bool  # True if target_path is within /tmp
    is_compound: bool

    @model_validator(mode="after")
    def secret_target_requires_path(self) -> "BashCommand":
        if self.target_is_secret and self.target_path is None:
            raise ValueError("target_is_secret=True requires target_path to be non-null")
        return self


class NetworkResource(BaseModel):
    """Resource entity for WebFetch and WebSearch tool calls."""

    model_config = ConfigDict(frozen=True)

    url: str
    domain: str
    in_allowlist: bool
    has_query_params: bool
    scheme: Literal["https", "http"]


# ---------------------------------------------------------------------------
# Entity builders
# ---------------------------------------------------------------------------

_WORKSPACE = Path("/workspace")


def build_agent_session(hook_input: dict) -> AgentSession:
    """Construct an AgentSession from a Claude Code hook input dict."""
    return AgentSession(
        session_id=hook_input.get("session_id") or hook_input.get("agent_id") or "",
    )


def build_file_path_entity(
    raw_path: str,
    secrets_patterns: list[tuple[str, list, str]],
    workspace: Path = _WORKSPACE,
) -> FilePath:
    """Build a FilePath entity from a raw path string.

    secrets_patterns is a list of (action, [compiled_re, ...], reason) tuples
    from load_secrets_patterns(), used to compute is_secret and is_safe_variant.
    """
    p = Path(raw_path)
    try:
        normalized = str(p.resolve())
    except Exception:
        normalized = raw_path

    try:
        rel = str(p.resolve().relative_to(workspace))
    except ValueError:
        rel = raw_path

    in_workspace = normalized.startswith(str(workspace))
    in_tmp = normalized.startswith("/tmp")

    # Control-plane detection: inside .contAIned/ or targeting managed-settings.json
    in_control_plane = False
    try:
        parts = Path(normalized).parts
        in_control_plane = ".contAIned" in parts or "managed-settings.json" in parts
    except Exception:
        pass

    extension = p.suffix.lower() if p.suffix else None

    is_secret = False
    is_safe_variant = False
    for action, patterns, _ in secrets_patterns:
        if any(pat.search(normalized) for pat in patterns):
            if action == "allow":
                is_safe_variant = True
                is_secret = True  # safe variants are a subset of secrets
            else:
                is_secret = True
            break

    return FilePath(
        raw_path=raw_path,
        normalized=normalized,
        in_workspace=in_workspace,
        in_tmp=in_tmp,
        is_secret=is_secret,
        is_safe_variant=is_safe_variant,
        in_control_plane=in_control_plane,
        extension=extension,
        relative_path=rel,
    )


def build_glob_pattern_entity(pattern: str) -> GlobPattern:
    """Build a GlobPattern entity from a raw glob pattern string.

    Extracts the longest non-wildcard directory prefix (for scope checks) and
    determines whether the pattern targets the workspace.

    Examples:
        "src/**/*.py"       -> prefix_path="src",           in_workspace=True
        "**/*.py"           -> prefix_path=None,            in_workspace=False
        "/workspace/src/*"  -> prefix_path="/workspace/src", in_workspace=True
        "/etc/hosts"        -> prefix_path="/etc/hosts",    in_workspace=False
    """
    m = _WILDCARD_RE.search(pattern)
    if m is None:
        # No wildcards — the whole pattern is a literal path/filename.
        prefix_path: str | None = pattern if pattern else None
    else:
        before = pattern[: m.start()]
        last_slash = before.rfind("/")
        if last_slash < 0:
            prefix_path = None
        else:
            prefix = before[:last_slash]
            prefix_path = prefix if prefix else None

    if prefix_path is None:
        in_workspace = False
    elif prefix_path.startswith("/workspace"):
        in_workspace = True
    elif prefix_path.startswith("/"):
        in_workspace = False
    else:
        # Relative pattern — assumed workspace-relative.
        in_workspace = True

    return GlobPattern(pattern=pattern, prefix_path=prefix_path, in_workspace=in_workspace)


def build_bash_command_entity(
    command: str,
    secrets_patterns: list[tuple[str, list, str]],
) -> BashCommand:
    """Build a BashCommand entity from a raw command string.

    Uses shlex tokenization — not naive string search — to detect shell
    operators. This correctly identifies && inside a quoted argument as part
    of the argument, not a compound operator (eliminating false positives).
    Compound detection uses shlex.shlex(punctuation_chars=True) which correctly
    handles operators adjacent to paths (e.g. "cd /tmp; ls") and groups
    multi-char operators ("&&", "||") as single tokens. shlex.split (used for
    verb/args decomposition) uses whitespace_split=True and would miss "cd /tmp;ls".
    Note: operators hidden inside quoted arguments passed to shell-delegation
    verbs (eval, bash -c, etc.) are false negatives — closed by the
    block-shell-delegation manifest rule.

    Token decomposition:
      - verb:            first token
      - subcommand:      second token if it doesn't start with "-"
      - args:            all tokens starting with "-" (flags / options)
      - positional_args: non-flag tokens after verb+subcommand
      - target_path:     first positional_arg that looks like a filesystem path

    kwargs (--flag value pairs) are not modelled in Phase 1 — the condition
    DSL lacks subscript notation. Add in Phase 2.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unmatched quote or other shlex error — treat conservatively.
        tokens = command.split()

    # Compound detection uses shlex.shlex with punctuation_chars=True so that
    # shell operators are tokenized correctly regardless of spacing:
    #   "cd /tmp; ls"  → ['cd', '/tmp', ';', 'ls']   (';' adjacent to path)
    #   "ls && echo"   → ['ls', '&&', 'echo']         ('&&' grouped correctly)
    # shlex.split (used above for decomposition) uses whitespace_split=True,
    # which treats "/tmp;" as a single token, hiding the semicolon.
    # punctuation_chars=True also keeps paths intact (adds ~-./*? to wordchars).
    try:
        _lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        is_compound = any(t in _SHELL_OPERATORS for t in _lex)
    except ValueError:
        # Fall back to whitespace-split tokens if shlex fails.
        is_compound = any(t in _SHELL_OPERATORS for t in tokens)
    verb = tokens[0] if tokens else ""

    # Subcommand: second token if it is not a flag AND not a filesystem path.
    # Path-like tokens (starting with / or ~, or containing /) are positional
    # args to the verb (e.g. "cat /etc/hosts"), not subcommands (e.g. "git push").
    subcommand: str | None = None
    rest_start = 1
    if len(tokens) > 1 and not tokens[1].startswith("-"):
        _t = tokens[1]
        _expanded = str(Path(_t).expanduser()) if _t.startswith("~") else _t
        _is_path = _expanded.startswith("/") or "/" in _t or _t in (".", "..")
        if not _is_path:
            subcommand = _t
            rest_start = 2

    # Partition remaining tokens into flags and positional args.
    rest_tokens = tokens[rest_start:]
    args: list[str] = []
    positional_args: list[str] = []
    skip_next = False
    for i, tok in enumerate(rest_tokens):
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            args.append(tok)
            # If the next token doesn't start with "-" and isn't shell-operator,
            # it's the value for this flag (kwarg pattern) — skip it from
            # positional_args but don't track as a kwarg in Phase 1.
            if (
                i + 1 < len(rest_tokens)
                and not rest_tokens[i + 1].startswith("-")
                and rest_tokens[i + 1] not in _SHELL_OPERATORS
            ):
                skip_next = True
        elif tok not in _SHELL_OPERATORS:
            positional_args.append(tok)

    # Extract target_path: first positional arg that looks like a filesystem path.
    # Recognises: absolute paths (/foo), paths with separators (./foo, ../bar),
    # and bare dot-components (. and ..) which are valid cd targets.
    target_path: str | None = None
    for tok in positional_args:
        expanded = str(Path(tok).expanduser()) if tok.startswith("~") else tok
        if expanded.startswith("/") or "/" in expanded or expanded in (".", ".."):
            target_path = expanded
            break

    # Check if target_path is a secret file.
    target_is_secret = False
    target_in_workspace = False
    target_in_tmp = False
    if target_path is not None:
        for action, patterns, _ in secrets_patterns:
            if any(pat.search(target_path) for pat in patterns):
                if action != "allow":
                    target_is_secret = True
                break
        try:
            resolved = str(Path(target_path).resolve())
        except Exception:
            resolved = target_path
        target_in_workspace = resolved.startswith(str(_WORKSPACE))
        target_in_tmp = resolved.startswith("/tmp")

    return BashCommand(
        raw=command,
        verb=verb,
        subcommand=subcommand,
        args=args,
        positional_args=positional_args,
        target_path=target_path,
        target_is_secret=target_is_secret,
        target_in_workspace=target_in_workspace,
        target_in_tmp=target_in_tmp,
        is_compound=is_compound,
    )


def build_network_resource_entity(
    url: str,
    allowed_domains: list[str],
) -> NetworkResource:
    """Build a NetworkResource entity from a URL string."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    scheme = parsed.scheme if parsed.scheme in ("https", "http") else "https"
    in_allowlist = domain in allowed_domains
    has_query_params = bool(parsed.query)
    return NetworkResource(
        url=url,
        domain=domain,
        in_allowlist=in_allowlist,
        has_query_params=has_query_params,
        scheme=scheme,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Per-tool target extraction
# ---------------------------------------------------------------------------


def extract_file_targets(tool_name: str, tool_input: dict) -> list[str]:
    """Return the list of file path targets for a tool call.

    MultiEdit carries multiple targets; all others carry one.
    Glob carries a pattern string — callers should build a GlobPattern entity
    with build_glob_pattern_entity() rather than a FilePath.
    Returns empty list if no target is present.
    """
    match tool_name:
        case "Read" | "Write" | "Edit":
            p = tool_input.get("file_path", "")
            return [p] if p else []
        case "MultiEdit":
            return [e["file_path"] for e in tool_input.get("edits", []) if e.get("file_path")]
        case "Glob":
            p = tool_input.get("pattern", "")
            return [p] if p else []
        case "Grep":
            p = tool_input.get("path", "")
            return [p] if p else []
        case _:
            return []


def is_glob_tool(tool_name: str) -> bool:
    """Return True if this tool's targets are glob patterns, not concrete paths."""
    return tool_name == "Glob"


# ---------------------------------------------------------------------------
# Context schema (Phase 3)
# ---------------------------------------------------------------------------

#: Declares every attribute that may appear in a context.* condition reference.
#: The validator uses this dict to validate context attribute names and types.
#: Values are Python type annotations (same format as Pydantic model field annotations).
CONTEXT_SCHEMA: dict[str, Any] = {
    "task_phase": Literal["initialization", "active", "qa", "review"],
    "qa_status": Literal["passing", "failing", "not_run", "unknown"],
    "tool_call_count": int,
}


def build_context(hook_input: dict) -> dict[str, Any]:
    """Build the context dict for policy evaluation from a Claude Code hook input.

    Populated at each hook call. Keys match CONTEXT_SCHEMA:
      task_phase:      Current task phase. Reads CONTAINED_TASK_PHASE env var;
                       defaults to "active".
      qa_status:       Last QA result for the active work unit ("passing",
                       "failing", "not_run", or "unknown").
      tool_call_count: Number of audit events for this session in tracer.db.

    All tracer reads are wrapped in try/except so hook startup is never blocked
    by a missing or locked database.
    """
    result: dict[str, Any] = {
        "task_phase": os.environ.get("CONTAINED_TASK_PHASE", "active"),
        "qa_status": "unknown",
        "tool_call_count": 0,
    }

    cwd = hook_input.get("cwd", ".")
    session_id = hook_input.get("session_id") or hook_input.get("agent_id") or ""
    if not session_id:
        return result

    db_path = Path(cwd) / ".contAIned" / "tracer.db"
    if not db_path.exists():
        return result

    try:
        conn = sqlite3.connect(str(db_path), timeout=0.5)
        try:
            # tool_call_count — count audit events for this session.
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                result["tool_call_count"] = row[0]

            # qa_status — from the most recent work unit associated with this session.
            row = conn.execute(
                """
                SELECT wu.qa_result
                FROM work_units wu
                JOIN work_unit_sessions wus ON wus.work_unit_id = wu.id
                WHERE wus.session_id = ?
                ORDER BY wu.opened_at DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row and row[0]:
                qa_result = json.loads(row[0])
                if qa_result.get("passed") is True:
                    result["qa_status"] = "passing"
                elif qa_result.get("passed") is False:
                    result["qa_status"] = "failing"
                else:
                    result["qa_status"] = "not_run"
        finally:
            conn.close()
    except Exception:
        pass

    return result
