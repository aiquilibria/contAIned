# Cedar-inspired policy engine for contAIned.
# Public re-exports for convenient hook imports.
from contained.engine.engine import evaluate
from contained.engine.entities import (
    CONTEXT_SCHEMA,
    AgentSession,
    BashCommand,
    Decision,
    FilePath,
    GlobPattern,
    NetworkResource,
    Outcome,
    Rule,
    build_agent_session,
    build_bash_command_entity,
    build_context,
    build_file_path_entity,
    build_glob_pattern_entity,
    build_network_resource_entity,
    extract_file_targets,
    is_glob_tool,
)
from contained.engine.policy import load_allowed_domains, load_rules, load_secrets_patterns

__all__ = [
    "evaluate",
    "CONTEXT_SCHEMA",
    "AgentSession",
    "BashCommand",
    "Decision",
    "FilePath",
    "GlobPattern",
    "NetworkResource",
    "Outcome",
    "Rule",
    "build_agent_session",
    "build_bash_command_entity",
    "build_context",
    "build_file_path_entity",
    "build_glob_pattern_entity",
    "build_network_resource_entity",
    "extract_file_targets",
    "is_glob_tool",
    "load_allowed_domains",
    "load_rules",
    "load_secrets_patterns",
]
