# CLAUDE.md — Hazel Codebase Guide

## What is Hazel?

Hazel is an ultra-lightweight personal AI assistant framework written in Python (v0.1.4.post5). It connects LLMs to chat platforms (Telegram, Discord, Slack, WhatsApp, etc.) via a message bus architecture, giving the LLM access to tools (file I/O, shell, web search, MCP) so it can act as an autonomous agent. PyPI package: `hazel-ai`. Requires Python >= 3.11.

## Quick Start

```bash
pip install hazel-ai
hazel onboard          # creates ~/.hazel/config.json + workspace
hazel agent -m "Hello" # one-shot CLI
hazel agent            # interactive REPL
hazel gateway          # long-running server with channels + cron + heartbeat
```

## Architecture Overview

```
User ──► Channel (Telegram/Discord/...) ──► MessageBus ──► AgentLoop ──► LLM Provider
                                               ▲                            │
                                               │                     tool calls
                                               │                            │
                                          OutboundMessage ◄── ToolRegistry ◄┘
```

**Core flow**: Channels receive user messages → push `InboundMessage` to `MessageBus` → `AgentLoop` consumes them → builds context (system prompt + history + memory) → calls LLM → executes tool calls in a loop → sends `OutboundMessage` back through the bus → `ChannelManager` dispatches to the correct channel.

## Directory Structure

```
hazel/
├── __main__.py              # Entry point: `python -m hazel`
├── __init__.py              # Version (__version__, __logo__)
├── agent/
│   ├── loop.py              # ★ AgentLoop — the core engine (message processing, tool loop)
│   ├── context.py           # ContextBuilder — system prompt assembly, message building
│   ├── memory.py            # MemoryStore + MemoryConsolidator — persistent memory system
│   ├── skills.py            # SkillsLoader — discovers and loads SKILL.md files
│   ├── subagent.py          # SubagentManager — background task execution
│   └── tools/
│       ├── base.py          # Tool ABC — all tools inherit from this
│       ├── registry.py      # ToolRegistry — dynamic tool registration + execution
│       ├── filesystem.py    # ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
│       ├── shell.py         # ExecTool — shell command execution with safety guards
│       ├── web.py           # WebSearchTool, WebFetchTool — web access with SSRF protection
│       ├── message.py       # MessageTool — send messages to chat channels
│       ├── spawn.py         # SpawnTool — spawn background subagents
│       ├── cron.py          # CronTool — manage scheduled jobs
│       ├── entity.py        # RecordChangeTool, QueryChangesTool, RetrieveEntitiesTool — entity memory
│       ├── intents.py       # Intent tools — task/reminder/event/followup management (SQLite-backed)
│       └── mcp.py           # MCPToolWrapper + connect_mcp_servers — MCP client integration
├── bus/
│   ├── events.py            # InboundMessage, OutboundMessage dataclasses
│   └── queue.py             # MessageBus — async queue decoupling channels from agent
├── channels/
│   ├── base.py              # BaseChannel ABC — all channels inherit from this
│   ├── manager.py           # ChannelManager — init/start/stop channels, dispatch outbound
│   ├── registry.py          # Auto-discovery (pkgutil + entry_points for plugins)
│   ├── telegram.py          # Telegram (python-telegram-bot)
│   ├── discord.py           # Discord (discord.py)
│   ├── slack.py             # Slack (slack_bolt)
│   ├── whatsapp.py          # WhatsApp (via Node.js bridge)
│   ├── feishu.py            # Feishu/Lark
│   ├── dingtalk.py          # DingTalk
│   ├── wecom.py             # WeCom (WeChat Work)
│   ├── matrix.py            # Matrix (nio)
│   ├── email.py             # Email (IMAP/SMTP)
│   ├── qq.py                # QQ
│   └── mochat.py            # MoChat
├── cli/
│   ├── commands.py          # ★ Typer CLI — onboard, gateway, agent, status, channels, provider login
│   ├── onboard_wizard.py    # Interactive setup wizard
│   └── model_info.py        # Model metadata for wizard
├── config/
│   ├── schema.py            # ★ Pydantic config schema (Config, ProvidersConfig, ChannelsConfig, etc.)
│   ├── loader.py            # load_config / save_config / migration
│   └── paths.py             # Runtime path helpers (data dir, media dir, cron dir, etc.)
├── providers/
│   ├── base.py              # LLMProvider ABC, LLMResponse, ToolCallRequest, GenerationSettings
│   ├── registry.py          # ★ ProviderSpec + PROVIDERS tuple — single source of truth for all providers
│   ├── litellm_provider.py  # LiteLLMProvider — main provider (routes through LiteLLM)
│   ├── custom_provider.py   # CustomProvider — direct OpenAI-compatible (bypasses LiteLLM)
│   ├── azure_openai_provider.py  # AzureOpenAIProvider
│   ├── openai_codex_provider.py  # OpenAI Codex (OAuth)
│   └── transcription.py     # Groq Whisper audio transcription
├── cron/
│   ├── service.py           # CronService — scheduled job execution (at/every/cron)
│   └── types.py             # CronJob, CronSchedule, CronPayload, CronStore dataclasses
├── heartbeat/
│   └── service.py           # HeartbeatService — periodic agent wake-up for HEARTBEAT.md tasks
├── security/
│   └── network.py           # SSRF protection — URL validation, private IP blocking
├── session/
│   └── manager.py           # Session, SessionManager — JSONL-based conversation persistence
├── utils/
│   ├── helpers.py           # Token estimation, message splitting, template sync (incl. entity dirs), timestamps
│   └── evaluator.py         # Post-run evaluation — decides if background task results should notify user
├── skills/                  # Built-in skills (each is a dir with SKILL.md)
│   ├── weather/             # Weather lookup
│   ├── github/              # GitHub operations
│   ├── cron/                # Cron usage guide
│   ├── memory/              # Memory management (always-on, describes all 4 layers)
│   ├── entity-retrieval/    # LLM-based CARD routing for semantic entity search
│   ├── index-ledger-write/  # Record changelog entries after entity creates/updates
│   ├── index-ledger-read/   # Query change ledger for "what changed" questions
│   ├── tmux/                # tmux session management
│   ├── clawhub/             # ClawHub skill marketplace
│   ├── summarize/           # Summarization
│   └── skill-creator/       # Create new skills (with scripts/)
└── templates/               # Workspace templates (synced on first run)
    ├── AGENTS.md            # Agent instructions (includes memory system policy)
    ├── ENTITY_TEMPLATE.md   # Entity file format template (CARD, Temporal, Facts, Notes)
    ├── SOUL.md              # Personality/values
    ├── USER.md              # User profile
    ├── TOOLS.md             # Tool usage notes
    ├── HEARTBEAT.md         # Periodic task list
    ├── scripts/
    │   └── generate-cards-index.sh  # Regenerates memory/_index/_cards.md from CARD headers
    └── memory/
        └── MEMORY.md        # Long-term memory template

bridge/                      # Node.js WhatsApp bridge (WebSocket ↔ Baileys)
├── src/
│   ├── server.ts            # BridgeServer — WS on 127.0.0.1, optional token auth
│   ├── whatsapp.ts          # WhatsAppClient — Baileys wrapper
│   └── index.ts             # Entry point
canvas/                      # Web dashboard for visualizing entity memory + intents
├── dashboard-server.js      # Node.js HTTP server (serves UI + REST API for SQLite)
├── dashboard.html           # Single-page dashboard (entity graph, intents, memory)
├── package.json             # Dependencies (better-sqlite3)
└── hazel-dashboard.service  # systemd unit file for persistent deployment
docs/
└── CHANNEL_PLUGIN_GUIDE.md  # How to write channel plugins
tests/                       # pytest test suite (Python 3.11–3.13)
```

## Key Components In-Depth

### AgentLoop (`agent/loop.py`)
The central engine. Key methods:
- `run()` — main event loop, consumes from bus, dispatches tasks
- `_process_message()` — handles one message: builds context → runs LLM loop → saves session
- `_run_agent_loop()` — the tool-calling iteration loop (up to `max_iterations` rounds)
- `_save_turn()` — persists new messages to session, strips runtime context, truncates large tool results
- `process_direct()` — for CLI/cron usage without the bus
- Handles `/new`, `/stop`, `/restart`, `/help` slash commands

### ContextBuilder (`agent/context.py`)
Assembles the system prompt from:
1. Identity section (workspace path, platform policy, guidelines)
2. Bootstrap files: `AGENTS.md`, `SOUL.md`, `USER.md`, `TOOLS.md` from workspace
3. Long-term memory from `memory/MEMORY.md`
4. Always-active skills
5. Skills summary (XML format, lazy-loaded by agent via `read_file`)

Also injects runtime context (current time, channel, chat ID) before each user message.

### Memory System (`agent/memory.py` + `agent/tools/entity.py`)
Four-layer persistent memory:

1. **Entity files** (`memory/areas/**/*.md`) — Structured state + knowledge about people, places, projects, domains, resources, and systems. Each file has a `<!-- CARD -->` header for routing, Temporal Constraints for state tracking, and append-only Facts. All files follow `ENTITY_TEMPLATE.md`.
2. **Daily logs** (`memory/YYYY-MM-DD.md`) — Raw chronological notes per day (grep-searchable, one file per day)
3. **Change ledger** (`memory/_index/changes.jsonl`) — Structured append-only log of all entity creates/updates, queried via `query_changes` tool
4. **Cards index** (`memory/_index/_cards.md`) — Auto-generated index of all entity CARD headers, used by `retrieve_entities` for LLM-based routing
5. **MEMORY.md** — Long-term facts (preferences, project context), always loaded into context, updated by LLM consolidation

Entity categories: `people/`, `places/`, `projects/`, `domains/`, `resources/`, `systems/` under `memory/areas/`.

`MemoryConsolidator` triggers when prompt tokens exceed the context window. It:
1. Picks a consolidation boundary at a user-turn edge
2. Sends the message chunk to LLM with a `save_memory` tool
3. LLM returns `history_entry` (appended to the current day's file) + `memory_update`
4. Falls back to raw archiving after 3 consecutive failures

### Provider System (`providers/`)
- **ProviderSpec** registry in `registry.py` is the single source of truth for all providers
- To add a new provider: add a `ProviderSpec` to `PROVIDERS` + a field to `ProvidersConfig` in `config/schema.py`
- Provider matching: explicit prefix → keyword match → local fallback → gateway fallback
- `LiteLLMProvider` handles most providers via LiteLLM
- `CustomProvider` bypasses LiteLLM for any OpenAI-compatible endpoint
- `AzureOpenAIProvider` and `OpenAICodexProvider` for specific providers
- Retry logic: 3 attempts with exponential backoff for transient errors (429, 5xx)

### Channel System (`channels/`)
- All channels extend `BaseChannel` (start/stop/send + `is_allowed` ACL)
- `ChannelManager` discovers channels via pkgutil scan + `entry_points` plugins
- Each channel config is stored as an extra field in `ChannelsConfig` (Pydantic `extra="allow"`)
- External plugins register via `hazel.channels` entry point group
- Progress streaming: channels receive `_progress` metadata for streaming partial responses

### Tool System (`agent/tools/`)
- All tools extend `Tool` ABC: `name`, `description`, `parameters` (JSON Schema), `execute(**kwargs)`
- `ToolRegistry` handles registration, validation, casting, and execution
- Tools auto-validate params against JSON Schema before execution
- Error results get `[Analyze the error above...]` hint appended
- Default tools: `read_file`, `write_file`, `edit_file`, `list_dir`, `exec`, `web_search`, `web_fetch`, `message`, `spawn`, `cron`, `record_change`, `query_changes`, `retrieve_entities`, `intent_create`, `intent_update`, `intent_get`, `intent_search`, `intent_complete`, `intent_snooze`, `intent_defer`, `intent_list_due`, `intent_sync_links`
- MCP tools are dynamically registered as `mcp_{server}_{tool}` wrappers

### Session System (`session/manager.py`)
- Sessions stored as JSONL files in `workspace/sessions/`
- First line is metadata (key, timestamps, `last_consolidated` offset)
- `get_history()` returns unconsolidated messages, aligned to legal tool-call boundaries
- Handles migration from legacy `~/.hazel/sessions/` path

### Cron System (`cron/`)
- Three schedule types: `at` (one-shot timestamp), `every` (interval), `cron` (cron expression with tz)
- Jobs stored in `cron/jobs.json` (auto-reloads on external modification)
- `on_job` callback runs the job through the agent loop
- Post-run evaluation decides whether to deliver results to user

### Entity Memory Tools (`agent/tools/entity.py`)
Three tools for structured entity tracking under `memory/areas/`:
- **`record_change`** — appends a structured change record to `memory/_index/changes.jsonl` after creating/updating an entity file. Auto-parses `entity_id`, `entity_type`, and `tags` from the file's `<!-- CARD -->` header if not provided. Has a configurable deduplication window (default 5s). Reason enum: `runtime`, `daily_compress`, `manual`, `import`.
- **`query_changes`** — deterministic query of the change ledger. Filters: `since`/`until` (time window), `entity_id`, `entity_type`, `path_prefix`, `reason`, `op`. Supports `sort` and `limit`.
- **`retrieve_entities`** — LLM-based CARD routing. Scans `memory/areas/**/*.md` for CARD headers, sends them to the agent's own LLM provider in an isolated call (low temperature, no tools), and returns the most relevant file paths for a query. The CARD index never enters the agent's main context window.

Entity types: `person`, `place`, `project`, `domain`, `resource`, `system`.

Entity files use CARD headers for metadata:
```markdown
<!-- CARD
id: person_alice
type: person
gist: Alice is the frontend lead, expert in React and design systems
tags: ["engineering", "frontend", "react"]
aliases: ["Alice Smith"]
links:
  - {rel: works_with, to: person_bob}
-->
```

Three supporting skills (`entity-retrieval`, `index-ledger-write`, `index-ledger-read`) guide the agent on when/how to use these tools. The `memory` skill (always-on) provides the high-level overview. All are auto-discovered builtin skills — no configuration needed.

### Intent System (`agent/tools/intents.py`)
SQLite-backed system for managing tasks, reminders, events, and followups. Database lives at `workspace/data/intents.db` (WAL mode, auto-created on first use).

Nine tools:
- **`intent_create`** — Create a new intent with optional entity links. Returns ULID-based ID. Types: `task`, `reminder`, `event`, `followup`. Priority 0-3, optional RRULE for recurrence.
- **`intent_update`** — Partial update of any field. Passing `links` replaces all links. Status changes refresh entity backlinks.
- **`intent_get`** — Fetch a single intent by ULID, including linked entities.
- **`intent_search`** — Full-text search (title/body) + filters: status, type, due/start windows, entity_path/entity_id. Supports limit/offset.
- **`intent_complete`** — Mark as done. For recurring intents (has `rrule` + `due_at`), advances to next occurrence via `dateutil.rrule` instead of completing.
- **`intent_snooze`** — Set `snooze_until` timestamp and status to `snoozed`. Snoozed intents are hidden from `intent_list_due` until the snooze expires.
- **`intent_defer`** — Clear `due_at`, increment `deferrals` counter, reset status to `active`.
- **`intent_list_due`** — Agenda/window queries. Returns intents due within a time window, with optional overdue inclusion. Excludes done/canceled. Respects snooze.
- **`intent_sync_links`** — Path rename handling (old_path → new_path), entity_id-based refresh, or full `refresh_all`. Call after renaming/moving entity files.

Key design:
- **Soft delete only**: Status transitions (`active` → `done`/`canceled`/`snoozed`), never SQL DELETE.
- **ULID IDs**: Sortable, timestamp-embedded, 26-char Crockford Base32 (generated without external dependency).
- **Bidirectional entity linking**: `intent_links` table stores intent↔entity relationships. Entity files get auto-managed `<!-- INTENTS:AUTO -->` sections showing linked active/snoozed intents.
- **Recurrence**: RFC 5545 RRULE strings parsed via `dateutil.rrule` (transitive dependency of `croniter`).
- **DB caching**: Module-level connection cache keyed by workspace path. WAL mode for concurrent reads.

### Heartbeat (`heartbeat/service.py`)
- Periodically reads `HEARTBEAT.md` from workspace
- Phase 1: LLM decides skip/run via virtual tool call
- Phase 2: If run, executes through agent loop
- Post-run evaluator gates whether to notify user

### Canvas Dashboard (`canvas/`)
Web-based visualization dashboard for the entity memory system, intents, and daily logs. Built as a standalone Node.js server that reads directly from the Hazel workspace.

**Quick start:**
```bash
cd canvas && npm install
node dashboard-server.js          # default port 8081
HAZEL_WORKSPACE=~/my-ws node dashboard-server.js 9000  # custom workspace + port
```

**Three tabs:**
1. **Entities** — Interactive knowledge graph (vis.js) showing entity files from `memory/areas/`. Parses `<!-- CARD -->` headers from `memory/_index/_cards.md`. Filterable by entity type (person, place, project, domain, resource, system). Click nodes to view full entity content in side panel.
2. **Intents** — List + calendar views for tasks/reminders/events/followups from `data/intents.db`. Supports filtering by status/type, search, sorting, and debug mode (shows all SQL columns). Calendar has month/week/day views with timezone support and RRULE expansion.
3. **Memory** — Daily logs viewer (expandable accordion of `memory/YYYY-MM-DD.md` files) and long-term memory viewer (`memory/MEMORY.md`). Markdown rendered via marked.js.

**API endpoints:**
| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard HTML |
| `GET /api/intents` | List intents (filters: `status`, `type`, `q`, `entity_path`, `limit`, `offset`) |
| `GET /api/intents/:id` | Single intent with linked entities |
| `GET /api/stats` | Intent counts by status/type, overdue, upcoming |
| `GET /api/changes` | Change ledger entries (filters: `since`, `until`, `entity_id`, `entity_type`, `limit`) |
| `GET /api/memory/daily` | List daily log dates |
| `GET /health` | Health check (status, workspace, db connection) |
| `GET /memory/*` | Static workspace files (entity files, cards index, daily logs, MEMORY.md) |

**Workspace resolution:** `HAZEL_WORKSPACE` env var → Hazel config (`~/.hazel/config.json` → `agents.defaults.workspace`) → default `~/.hazel/workspace`.

**Systemd deployment:** Copy `hazel-dashboard.service` to `/etc/systemd/system/`, adjust paths, then `systemctl enable --now hazel-dashboard`.

## Configuration

Config file: `~/.hazel/config.json` (or custom path via `--config`). Also supports env vars with `HAZEL_` prefix and `__` nesting.

Key config sections:
- `agents.defaults` — model, provider, workspace, max_tokens, context_window_tokens, temperature, reasoning_effort
- `providers` — API keys/bases for each provider (anthropic, openai, openrouter, deepseek, gemini, ollama, etc.)
- `channels` — per-channel config (enabled, allowFrom, token, etc.)
- `gateway` — host, port, heartbeat settings
- `tools` — web proxy, search provider, exec settings, MCP servers, restrict_to_workspace

Config schema accepts both camelCase and snake_case keys.

## How to Add Things

### Add a new tool
1. Create a class extending `Tool` in `agent/tools/`
2. Implement `name`, `description`, `parameters`, `execute(**kwargs)`
3. Register it in `AgentLoop._register_default_tools()`

### Add a new channel
1. Create a class extending `BaseChannel` in `channels/`
2. Implement `start()`, `stop()`, `send()` — use `_handle_message()` for inbound
3. Set `name` and `display_name` class attributes
4. The registry auto-discovers it via pkgutil scan (no manual registration needed)
5. Add a `default_config()` classmethod for onboard auto-population

### Add a new LLM provider
1. Add a `ProviderSpec` to the `PROVIDERS` tuple in `providers/registry.py`
2. Add a matching field to `ProvidersConfig` in `config/schema.py`
3. That's it — LiteLLM handles the actual API calls

### Add a new skill
1. Create `hazel/skills/{name}/SKILL.md` (built-in) or `workspace/skills/{name}/SKILL.md` (user)
2. Add YAML frontmatter with `name`, `description`, optional `metadata` (JSON with `requires`, `always`)
3. The agent discovers it automatically and can read it on demand

## Testing

```bash
pip install .[dev]
python -m pytest tests/ -v
```

Tests run on Python 3.11, 3.12, 3.13. CI is in `.github/workflows/ci.yml`.

## Deployment

- **Docker**: `docker-compose.yml` runs `hazel gateway` with volume mount to `~/.hazel`
- **Direct**: `hazel gateway --config /path/to/config.json`
- Gateway port default: 18790

## Important Patterns

- **Message bus decoupling**: Channels never talk to the agent directly. Everything goes through `MessageBus` async queues.
- **Session keys**: Format is `channel:chat_id` (e.g., `telegram:123456`). CLI uses `cli:direct`.
- **Progress streaming**: During tool execution, partial results are sent as `_progress` metadata messages.
- **Subagents**: The `spawn` tool creates background agents with limited tools (no message, no spawn). Results are announced back via system messages on the bus.
- **SSRF protection**: `web_fetch` and `exec` validate URLs against private IP ranges.
- **Tool result truncation**: Results > 16,000 chars are truncated in session history.
- **Context window management**: `MemoryConsolidator` keeps prompt size under half the context window by consolidating old messages.
- **Entity memory lifecycle**: Agent detects durable signal → creates/updates entity file in `memory/areas/` per `ENTITY_TEMPLATE.md` → calls `record_change` to log to ledger → runs `generate-cards-index.sh` to refresh cards index. All workspace directories are auto-created by `sync_workspace_templates` on first run.
- **Config camelCase**: Schema uses `alias_generator=to_camel` so JSON uses camelCase but Python uses snake_case.

## Cutting a Release

Releases are built and published automatically by CI (`.github/workflows/release.yml`) when a `v*` tag is pushed.

```bash
# 1. Bump version in BOTH files (they must match):
#    - pyproject.toml        → version = "X.Y.Z"
#    - hazel/__init__.py     → __version__ = "X.Y.Z"

# 2. Commit the version bump
git add pyproject.toml hazel/__init__.py
git commit -m "chore: bump version to X.Y.Z"
git push

# 3. Tag and push — this triggers the release workflow
git tag vX.Y.Z
git push origin vX.Y.Z
```

The workflow will:
- Build a wheel and sdist via `python -m build`
- Create a GitHub Release with auto-generated notes (`gh release create --generate-notes`)
- Attach the `.whl` and `.tar.gz` to the release

Tag format: `v{version}` (e.g. `v0.1.5.post9`). The workflow is idempotent — re-running on the same tag will overwrite artifacts without error.

**Important:** Always `git push` the commit *before* pushing the tag. If you push the tag first, the workflow checks out a commit that may not exist on the remote yet, and the wheel build will fail or produce stale artifacts.
