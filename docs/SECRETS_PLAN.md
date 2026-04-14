# Unified Secrets System — Implementation Plan

**Status:** Design locked, ready to implement.
**Prerequisite reading:** `CLAUDE.md` (Architecture Overview), `hazel/config/schema.py` (ProvidersConfig, MCPServerConfig).

## Goal

One mechanism for every sensitive value Hazel ever needs — API keys, OAuth
tokens, browser-redirect results, MCP auth headers, skill credentials. The
LLM never sees the raw value.

One command to set: `hazel auth <name>`
One place to store: `~/.hazel/secrets/<name>` (chmod `0600`)
Three read paths, all resolving the same file.

## Non-goals (v1)

- Encryption at rest. `chmod 0600` + OS-level account isolation is the
  baseline; keyring integration is a later add.
- Real-time notification from `hazel auth` back to a running agent
  session. The agent polls (or the user re-runs the skill) once auth
  is done.
- Per-skill scoped injection. v1 injects all secrets into every
  subprocess via `HAZEL_SECRET_<NAME>`. Scoping is a later tightening.
- Migrating existing `config.json` provider API keys. Those keep
  working as-is; the new system is additive for new secrets.

## Data model

### Storage layout
```
~/.hazel/
├── config.json            # existing; provider keys stay here for back-compat
└── secrets/               # NEW, chmod 0700
    ├── gmail              # NEW, chmod 0600, one file per secret, raw value
    ├── github             # ...
    └── openweather        # ...
```

### Naming
- Lowercase, `a-z0-9_-`. Enforced by validator.
- Examples: `gmail`, `google_calendar`, `openweather`, `slack_bot`,
  `my_custom_mcp_bearer`.

## Code surface

### New files

**`hazel/secrets/__init__.py`** — public API, re-exports from store.
```python
from hazel.secrets.store import get, set, exists, delete, list_names, path_for
```

**`hazel/secrets/store.py`** — file-backed store.
```python
class SecretMissingError(KeyError): ...

def get(name: str) -> str: ...          # raises SecretMissingError
def get_or_none(name: str) -> str|None: ...
def set(name: str, value: str) -> None: ...  # chmods 0600, creates dir 0700
def exists(name: str) -> bool: ...
def delete(name: str) -> bool: ...
def list_names() -> list[str]: ...
def path_for(name: str) -> Path: ...
def _validate_name(name: str) -> None: ...  # regex [a-z0-9_-]+, length<=64
```

**`hazel/secrets/registry.py`** — known OAuth services.
```python
# name -> callable that does the OAuth flow and returns the token
_OAUTH_PROVIDERS: dict[str, Callable[[], str]] = {
    "gmail": _oauth_google_gmail,
    "google_calendar": _oauth_google_calendar,
    "github": _oauth_github,
    # extend here as services are added
}

def has_oauth(name: str) -> bool: ...
def run_oauth(name: str) -> str: ...  # returns token, raises on failure
```
Each provider is a small function wrapping `oauth_cli_kit.login_oauth_interactive`
(same pattern as `commands.py:2407-2423`). Start with just one OAuth
implementation (pick `github` since it's already in use for copilot).
Add others as needed — the registry makes it trivial.

**`hazel/cli/auth.py`** — the `hazel auth <name>` command.
```python
@app.command("auth")
def auth(
    name: str = typer.Argument(..., help="Secret name (e.g. gmail, openweather)"),
    remove: bool = typer.Option(False, "--remove", help="Delete the secret instead"),
    from_env: str = typer.Option(None, "--from-env", help="Copy from named env var"),
    show: bool = typer.Option(False, "--show", help="Print to stdout (dangerous)"),
):
    ...
```

Flow:
1. If `--remove`: call `store.delete(name)`, print confirmation.
2. If `--from-env`: read `os.environ[VAR]`, call `store.set(name, value)`.
3. Otherwise:
   - If `registry.has_oauth(name)` → `registry.run_oauth(name)` → `store.set`.
   - Else → `getpass.getpass(f"Paste value for {name}: ")` → `store.set`.
4. Print "✓ Secret saved as `<name>`".
5. Never echo the value; `--show` is an explicit opt-in escape hatch.

Also add `hazel secret list` (prints names only, never values).

**`hazel/agent/tools/secrets.py`** — `request_secret` agent tool.
```python
class RequestSecretTool(Tool):
    name = "request_secret"
    description = """Request that the user provides a credential Hazel needs
    (API key, OAuth token, etc). You NEVER see the value — you just tell the
    user which command to run, and this tool tracks whether they've done it.

    Use this whenever you hit anything sensitive during setup or runtime.
    Do NOT ask the user to paste credentials in chat."""
    # params: name (required), purpose (required)
    # returns: {"status": "ready"|"missing", "command": "hazel auth <name>"}
```

The execute() body:
- `if store.exists(name): return {"status": "ready"}`
- else: return `{"status": "missing", "command": f"hazel auth {name}",
   "message": f"Tell the user to run: hazel auth {name} (for: {purpose})"}`

The agent learns from the system prompt: "when you see `missing`, output
the command to the user and move on — don't stall, don't retry in a loop,
don't paste credentials yourself."

### Modified files

**`hazel/config/paths.py`** — add:
```python
def get_secrets_dir() -> Path:
    d = Path.home() / ".hazel" / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d
```

**`hazel/config/loader.py`** — resolve `@secret:<name>` references.
When loading config, walk the dict and for any string value matching
`^@secret:([a-z0-9_-]+)$`, replace with `store.get(name)`. If missing,
leave the placeholder and log a warning (don't crash — user may not
have authed yet).

This lets `config.json` reference secrets without storing them:
```json
{
  "tools": {
    "mcp_servers": {
      "gmail": {
        "env": { "GMAIL_TOKEN": "@secret:gmail" }
      }
    }
  }
}
```

**`hazel/agent/loop.py`** — register `RequestSecretTool` in
`_register_default_tools()`.

**`hazel/agent/tools/shell.py`** — auto-inject secrets as env vars.
In `execute()`, after `env = os.environ.copy()`:
```python
for name in store.list_names():
    env[f"HAZEL_SECRET_{name.upper()}"] = store.get(name)
```
(Subprocess env is process-private; not logged.)

**`hazel/agent/context.py`** — add a short fixed block to the system
prompt (in `ContextBuilder`, same level as the identity section). One
paragraph: "Whenever you need a sensitive value (API key, OAuth token,
password, webhook URL with credentials), call `request_secret`. Never
ask the user to paste secrets in chat. If `request_secret` returns
`missing`, tell the user the exact command to run and continue with
the rest of the task."

This is permanent, not just onboarding — unlike `AGENT_IDENTITY.md`.

**`hazel/cli/commands.py`** — wire up `auth.py` (typer subcommand or
direct `@app.command` registration). If `openai_codex` / `github_copilot`
login flows still exist separately, leave them for now (they're
provider-specific and pre-date this system).

## Integration with existing flows

### `setup-skills` (from recent work)
Agent calls `request_secret("gmail", "gmail skill needs OAuth")`.
If missing, the tool suggests `hazel auth gmail`. The agent either:
- Tells the user directly ("Run `hazel auth gmail`")
- Uses `queue_user_action(title="Authenticate Gmail", description="Run `hazel auth gmail`")` — which is already a tool we registered.

These two tools (`request_secret` and `queue_user_action`) are
complementary: `request_secret` checks if the secret is here NOW,
`queue_user_action` records a deferred follow-up. The agent uses
whichever fits.

### MCP servers
Users can set `"env": {"TOKEN": "@secret:my_mcp"}` in config.json,
run `hazel auth my_mcp`, and the loader resolves the reference at
runtime. The old pattern (raw value in `env`) keeps working.

### Skills (both markdown and script-based)
- **Markdown skills** — SKILL.md instructions say "call `request_secret`"
  and the agent obeys.
- **Script skills** — `from hazel.secrets import get; token = get("foo")`.
  If missing, raise a clear error mentioning `hazel auth foo`. The
  agent sees the exec error, reacts accordingly.

## Implementation order

Do these strictly in sequence; each step should pass tests before the
next starts.

1. **Store only.** `hazel/secrets/store.py` + `paths.py` helper + tests
   for `get/set/exists/delete/list_names`, name validation, permissions.
   No CLI, no tool, no config resolver yet.
2. **`@secret:` config resolver.** Add to `loader.py` with tests.
   Back-compat: nothing changes for users not using `@secret:`.
3. **`hazel auth` command (plain-key path only).** No OAuth yet —
   just `getpass` + `store.set`. Plus `hazel secret list` and
   `hazel auth --remove`. Tests invoke via typer runner.
4. **`RequestSecretTool` + agent loop registration + system prompt
   line.** Tests cover the tool returning ready/missing.
5. **Exec tool env injection.** Add the `HAZEL_SECRET_*` loop. Test
   that a shell command can read them.
6. **OAuth registry + one provider.** Pick `github` (we already use
   `oauth_cli_kit` for copilot — reuse the pattern). Test that
   `hazel auth github` routes to OAuth instead of getpass.
7. **Add remaining OAuth providers as needed.** Gmail, Google Calendar,
   Slack. Each is ~20 lines.

Ship as `v0.1.6.post15` or bump to `v0.1.7` after step 6.

## Edge cases to handle

- **Re-auth of existing secret**: `hazel auth gmail` when gmail exists →
  prompt "Overwrite existing secret? [y/N]", default no. `--force` to skip.
- **Missing `oauth_cli_kit`**: if import fails and the name is in the
  OAuth registry, fall back to getpass with a warning.
- **`@secret:foo` where foo is missing**: keep the placeholder, log a
  warning, return `None` from `get_api_key()` so the relevant feature
  is gracefully disabled rather than crashing the app.
- **Secret contains newlines** (e.g. a PEM): `store.set` strips only
  trailing newline from the OAuth result; raw bytes preserved
  otherwise.
- **Windows perms**: `os.chmod` on Windows is best-effort; document
  that POSIX perms don't protect Windows users and we rely on the
  user account boundary there.
- **Concurrent writes**: unlikely but possible. Use `os.replace` (atomic
  rename) pattern in `store.set`.

## Tests to add

- `tests/test_secrets_store.py` — store API, name validation, perms.
- `tests/test_secrets_config_resolver.py` — `@secret:` resolution in
  loaded config, missing-secret graceful degradation.
- `tests/test_secrets_cli.py` — `hazel auth`, `hazel secret list`,
  `--remove`, `--from-env`, overwrite confirmation.
- `tests/test_secrets_agent_tool.py` — `RequestSecretTool` ready/missing
  return shapes, name validation.
- `tests/test_exec_secret_injection.py` — shell can read
  `HAZEL_SECRET_*` env vars.

## What gets touched, one-line recap

| File | Change |
|---|---|
| `hazel/secrets/store.py` | NEW — file-backed store |
| `hazel/secrets/registry.py` | NEW — OAuth provider registry |
| `hazel/secrets/__init__.py` | NEW — public re-exports |
| `hazel/cli/auth.py` | NEW — `hazel auth` + `hazel secret list` |
| `hazel/agent/tools/secrets.py` | NEW — `RequestSecretTool` |
| `hazel/config/paths.py` | `get_secrets_dir()` helper |
| `hazel/config/loader.py` | `@secret:` resolver |
| `hazel/agent/loop.py` | register `RequestSecretTool` |
| `hazel/agent/tools/shell.py` | `HAZEL_SECRET_*` env injection |
| `hazel/agent/context.py` | one paragraph in system prompt |
| `hazel/cli/commands.py` | wire `auth.py` typer commands |
| `tests/test_secrets_*.py` | new test files |
| `CLAUDE.md` | add "Secrets" subsection under Architecture |

## Success criteria

After implementation:
- A user can run `hazel auth gmail`, complete OAuth in the browser,
  and their token is at `~/.hazel/secrets/gmail` chmod 0600.
- Any skill or MCP can read that token via `get("gmail")`, env var
  injection, or `@secret:gmail` in config.json.
- The LLM can call `request_secret("gmail", ...)` to check availability
  but never receives the value.
- The LLM's system prompt instructs it to use `request_secret` for
  anything sensitive and never ask for pastes in chat.
- `hazel secret list` shows names only; there is no CLI path that
  prints a value except the opt-in `hazel auth <name> --show`.
