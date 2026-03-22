# contAIned plugin

Built-in Claude Code plugin shipped with every contAIned runtime image. Provides
the `/contained:tracer` skill backed by a tracer MCP server for querying
session history, audit logs, and file diffs stored in `.contAIned/tracer.db`.
Invocable directly by the operator (`/contained:tracer <question>`) and
auto-invoked by the agent when the task requires tracer data.

## Skill

### `/contained:tracer [question]`

Ask any natural-language question about the current or past agent sessions. Type
`/contained:tracer` to invoke it directly, or Claude will use it automatically
when answering questions about session history.

```
/contained:tracer show me the last 5 sessions
/contained:tracer what files changed in session abc123?
/contained:tracer were there any denied tool calls today?
/contained:tracer show me the diff for src/contained/init.py from the last session
/contained:tracer which sessions had QA failures?
```

The skill is available immediately in every contAIned session — no installation or
configuration required.

## MCP tools

The tracer MCP server (`contained.tracer_mcp`) exposes two tools that Claude uses
to answer queries. You can also call them directly via the MCP tool interface.

| Tool | Description |
|---|---|
| `mcp__plugin_contained_tracer__get_schema` | Returns the tracer database schema. Call this first to understand available tables and columns. |
| `mcp__plugin_contained_tracer__query_tracer` | Runs a read-only SQL `SELECT` against `tracer.db`. Returns up to 500 rows. |

Both tools are auto-approved (no operator confirmation required) — they are
read-only and access only the session's own tracer database.

## Database schema

Key tables in `.contAIned/tracer.db`:

| Table | Contents |
|---|---|
| `tasks` | One row per agent session: `session_id`, `prompt`, `status`, `started_at`, `ended_at`, `summary` (JSON), `narrative` (JSON) |
| `audit_events` | Every tool call: `ts`, `session_id`, `tool`, `input` (JSON), `outcome`, `reason` |
| `snapshots` | File write events with content hashes, used to reconstruct diffs |
| `baselines` | Pre-task file state — `pre_hash` is `NULL` for new files |
| `blobs` | Content-addressed store for file snapshots |

Use `get_schema` to see the full column list for any table.

## Plugin structure

```
plugin/
├── .claude-plugin/
│   └── plugin.json        # plugin manifest (name: "contained")
├── .mcp.json              # registers the tracer MCP server
├── skills/
│   └── tracer/
│       └── SKILL.md       # contained:tracer agent skill definition
└── README.md              # this file
```

The MCP server process (`contained.tracer_mcp`) is started automatically by
Claude Code when the plugin loads. It reads the database path from the
`CONTAINED_DB_PATH` environment variable (set by the contAIned entrypoint) or
falls back to `/workspace/.contAIned/tracer.db`.
