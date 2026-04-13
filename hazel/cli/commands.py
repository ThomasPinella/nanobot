"""CLI commands for Hazel."""

import asyncio
from contextlib import contextmanager, nullcontext
import os
import select
import signal
import sys
from pathlib import Path
from typing import Any

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from hazel import __logo__, __version__
from hazel.config.paths import get_workspace_path
from hazel.config.schema import Config
from hazel.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="hazel",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} Hazel - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from hazel.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} Hazel[/cyan]")
    console.print(body)
    console.print()


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(response: str, render_markdown: bool) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} Hazel[/cyan]"),
                c.print(Markdown(content) if render_markdown else Text(content)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


class _ThinkingSpinner:
    """Spinner wrapper with pause support for clean progress output."""

    def __init__(self, enabled: bool):
        self._spinner = console.status(
            "[dim]Hazel is thinking...[/dim]", spinner="dots"
        ) if enabled else None
        self._active = False

    def __enter__(self):
        if self._spinner:
            self._spinner.start()
        self._active = True
        return self

    def __exit__(self, *exc):
        self._active = False
        if self._spinner:
            self._spinner.stop()
        return False

    @contextmanager
    def pause(self):
        """Temporarily stop spinner while printing progress."""
        if self._spinner and self._active:
            self._spinner.stop()
        try:
            yield
        finally:
            if self._spinner and self._active:
                self._spinner.start()


def _print_cli_progress_line(text: str, thinking: _ThinkingSpinner | None) -> None:
    """Print a CLI progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        console.print(f"  [dim]↳ {text}[/dim]")


async def _print_interactive_progress_line(text: str, thinking: _ThinkingSpinner | None) -> None:
    """Print an interactive progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        await _print_interactive_line(text)


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} Hazel v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """Hazel - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive wizard"),
):
    """Initialize Hazel configuration and workspace."""
    from hazel.config.loader import get_config_path, load_config, save_config, set_config_path
    from hazel.config.schema import Config

    if config:
        config_path = Path(config).expanduser().resolve()
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    # Create or update config
    if config_path.exists():
        if wizard:
            config = _apply_workspace_override(load_config(config_path))
        else:
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
            console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
            if typer.confirm("Overwrite?"):
                config = _apply_workspace_override(Config())
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            else:
                config = _apply_workspace_override(load_config(config_path))
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        config = _apply_workspace_override(Config())
        # In wizard mode, don't save yet - the wizard will handle saving if should_save=True
        if not wizard:
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    # Run interactive wizard if enabled
    if wizard:
        from hazel.cli.onboard_wizard import run_onboard

        try:
            result = run_onboard(initial_config=config)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            config = result.config
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'hazel onboard' again to complete setup.[/yellow]")
            raise typer.Exit(1)
    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    # Set up canvas dashboard
    _setup_dashboard(config)

    agent_cmd = 'hazel agent -m "Hello!"'
    gateway_cmd = "hazel gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} Hazel is ready!")
    console.print("\nNext steps:")
    if wizard:
        console.print(f"  1. Chat: [cyan]{agent_cmd}[/cyan]")
        console.print(f"  2. Start gateway: [cyan]{gateway_cmd}[/cyan]")
    else:
        console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
        console.print("     Get one at: https://openrouter.ai/keys")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")
    dashboard_cfg = config.gateway.dashboard
    if dashboard_cfg.enabled:
        console.print(f"\n  Dashboard: [cyan]http://{dashboard_cfg.host}:{dashboard_cfg.port}[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/hazel#-chat-apps[/dim]")


def _fetch_setup_config(token: str) -> dict[str, str] | None:
    """Fetch setup config JSON from the config API.

    Returns a dict with keys: skillsSetup, userActions, agentIdentity.
    Returns None if the fetch fails.
    """
    import json
    import urllib.request
    import urllib.error

    base_url = os.environ.get(
        "HAZEL_CONFIG_URL", "https://get-hazel.vercel.app"
    ).rstrip("/")
    url = f"{base_url}/api/config/{token}"

    console.print(f"[dim]Fetching setup config from {url}...[/dim]")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        console.print(f"[red]Failed to fetch setup config: HTTP {e.code}[/red]")
        return None
    except Exception as e:
        console.print(f"[red]Failed to fetch setup config: {e}[/red]")
        return None

    # Validate expected keys
    expected = {"skillsSetup", "userActions", "agentIdentity"}
    missing = expected - set(data.keys())
    if missing:
        console.print(f"[yellow]Warning: setup config missing keys: {', '.join(missing)}[/yellow]")

    console.print("[green]✓[/green] Setup config loaded")
    return data


@app.command()
def quickstart(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    setup_config: str | None = typer.Option(None, "--setup-config", help="Setup config token for automated setup"),
):
    """Get Hazel running in 2 minutes with sensible defaults."""
    from hazel.cli.quickstart import run_quickstart, run_quickstart_post_save
    from hazel.config.loader import get_config_path, load_config, save_config, set_config_path

    if config:
        config_path = Path(config).expanduser().resolve()
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    # Fetch setup config if token provided
    setup_config_data = None
    if setup_config:
        setup_config_data = _fetch_setup_config(setup_config)

    # Load existing or create fresh config
    if config_path.exists():
        cfg = load_config(config_path)
    else:
        cfg = Config()

    if workspace:
        cfg.agents.defaults.workspace = workspace

    try:
        cfg, should_save = run_quickstart(cfg)
    except Exception as e:
        console.print(f"[red]✗[/red] Error during quickstart: {e}")
        raise typer.Exit(1)

    if not should_save:
        console.print("[yellow]No changes saved.[/yellow]")
        raise typer.Exit()

    save_config(cfg, config_path)
    console.print(f"[green]✓[/green] Config saved at {config_path}")

    _onboard_plugins(config_path)

    # Create workspace
    workspace_path = get_workspace_path(cfg.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    # Save agent identity to workspace if present in setup config
    if setup_config_data and setup_config_data.get("agentIdentity"):
        identity_path = workspace_path / "AGENT_IDENTITY.md"
        identity_path.write_text(setup_config_data["agentIdentity"], encoding="utf-8")
        console.print("[green]✓[/green] Agent identity saved")

    # Dashboard setup
    _setup_dashboard(cfg)

    # Optional setup-skills + setup-user-actions (auto-fed if setup config present)
    run_quickstart_post_save(cfg, setup_config_data=setup_config_data)

    agent_cmd = 'hazel agent -m "Hello!"'
    gateway_cmd = "hazel gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} Hazel is ready!")
    console.print("\nNext steps:")
    console.print(f"  1. Chat: [cyan]{agent_cmd}[/cyan]")
    console.print(f"  2. Start gateway: [cyan]{gateway_cmd}[/cyan]")

    dashboard_cfg = cfg.gateway.dashboard
    if dashboard_cfg.enabled:
        console.print(f"\n  Dashboard: [cyan]http://{dashboard_cfg.host}:{dashboard_cfg.port}[/cyan]")


def _run_setup_skills(cfg: Config, instructions: str) -> None:
    """Feed setup instructions to the agent for execution."""
    from hazel.agent.loop import AgentLoop
    from hazel.bus.queue import MessageBus

    bus = MessageBus()
    provider = _make_provider(cfg)

    sync_workspace_templates(cfg.workspace_path)

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=cfg.workspace_path,
        model=cfg.agents.defaults.model,
        max_iterations=cfg.agents.defaults.max_tool_iterations,
        context_window_tokens=cfg.agents.defaults.context_window_tokens,
        web_search_config=cfg.tools.web.search,
        web_proxy=cfg.tools.web.proxy or None,
        exec_config=cfg.tools.exec,
        restrict_to_workspace=cfg.tools.restrict_to_workspace,
        mcp_servers=cfg.tools.mcp_servers,
        channels_config=cfg.channels,
        dashboard_config=cfg.gateway.dashboard,
    )

    import asyncio

    async def _run():
        prompt = (
            "You are running a setup-skills step during Hazel onboarding. "
            "Follow the instructions below exactly. Execute any shell commands, "
            "create any files, and install any packages as described. "
            "Work in the workspace directory. Report what you did when done.\n\n"
            f"{instructions}"
        )
        response = await agent_loop.process_direct(
            prompt,
            session_key="cli:setup-skills",
            channel="cli",
            chat_id="direct",
        )
        await agent_loop.close_mcp()
        return response

    console.print()
    console.print("[dim]Running setup instructions...[/dim]")
    response = asyncio.run(_run())
    if response:
        from rich.markdown import Markdown as RichMarkdown
        console.print()
        console.print(RichMarkdown(response))


@app.command(name="setup-skills")
def setup_skills(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Run setup instructions to install skills and configure the workspace.

    If saved instructions exist from a prior install, they are used automatically.
    Otherwise, paste your setup instructions into the terminal and press Enter.
    """
    from hazel.config.paths import get_pending_setup_skills_path

    cfg = _load_runtime_config(config, workspace)

    # Check that an LLM provider is configured
    if not cfg.get_api_key():
        console.print("[red]Error: No LLM provider is configured.[/red]")
        console.print("Set up a provider first with [cyan]hazel quickstart[/cyan] or [cyan]hazel onboard --wizard[/cyan]")
        raise typer.Exit(1)

    # Check for saved pending instructions from install
    pending_path = get_pending_setup_skills_path()
    if pending_path.exists():
        instructions = pending_path.read_text(encoding="utf-8").strip()
        if instructions:
            console.print("[green]✓[/green] Found saved skills setup instructions from install.")
            _run_setup_skills(cfg, instructions)
            pending_path.unlink()
            return

    console.print("Paste your setup instructions below and press [bold]Enter[/bold]:\n")
    try:
        import questionary
        instructions = questionary.text("Instructions:", qmark=">", multiline=False).ask()
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        raise typer.Exit()
    if instructions is None:
        raise typer.Exit()

    if not instructions.strip():
        console.print("[yellow]No instructions provided.[/yellow]")
        raise typer.Exit()

    _run_setup_skills(cfg, instructions)


def _run_setup_user_actions(cfg: Config, initial_message: str) -> None:
    """Run an interactive setup dialogue for user actions."""
    from hazel.agent.loop import AgentLoop
    from hazel.bus.queue import MessageBus

    bus = MessageBus()
    provider = _make_provider(cfg)

    sync_workspace_templates(cfg.workspace_path)

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=cfg.workspace_path,
        model=cfg.agents.defaults.model,
        max_iterations=cfg.agents.defaults.max_tool_iterations,
        context_window_tokens=cfg.agents.defaults.context_window_tokens,
        web_search_config=cfg.tools.web.search,
        web_proxy=cfg.tools.web.proxy or None,
        exec_config=cfg.tools.exec,
        restrict_to_workspace=cfg.tools.restrict_to_workspace,
        mcp_servers=cfg.tools.mcp_servers,
        channels_config=cfg.channels,
        dashboard_config=cfg.gateway.dashboard,
    )

    # Build a stripped-down system prompt: identity + skills, no bootstrap files or memory
    context = agent_loop.context
    workspace_path = str(cfg.workspace_path.expanduser().resolve())

    prompt_parts = [
        f"""# Hazel — Setup User Actions

You are Hazel, running an interactive setup session. The user has provided instructions
describing actions, workflows, skills, or automations they want configured.

## Workspace
Your workspace is at: {workspace_path}
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md
- Memory: {workspace_path}/memory/

## Guidelines
- Walk through the user's instructions step by step.
- Ask clarifying questions when something is ambiguous.
- Use your tools to accomplish each setup task (file operations, shell commands, web search, etc.).
- When all steps are complete, clearly tell the user that setup is done and summarize what was configured.
- Be concise and action-oriented.""",
    ]

    always_skills = context.skills.get_always_skills()
    if always_skills:
        always_content = context.skills.load_skills_for_context(always_skills)
        if always_content:
            prompt_parts.append(f"# Active Skills\n\n{always_content}")

    skills_summary = context.skills.build_skills_summary()
    if skills_summary:
        prompt_parts.append(
            "# Skills\n\n"
            "The following skills extend your capabilities. "
            "To use a skill, read its SKILL.md file using the read_file tool.\n\n"
            f"{skills_summary}"
        )

    system_prompt = "\n\n---\n\n".join(prompt_parts)
    session_key = "cli:setup-user-actions"

    _init_prompt_session()

    console.print()
    console.print("[dim]Starting interactive setup session...[/dim]")
    console.print("[dim]Type 'exit' or press Ctrl+C when done.[/dim]\n")

    async def _run():
        _thinking = None

        async def _progress(content: str, *, tool_hint: bool = False) -> None:
            _print_cli_progress_line(content, _thinking)

        # Process initial message
        _thinking = _ThinkingSpinner(enabled=True)
        with _thinking:
            response = await agent_loop.process_direct(
                initial_message,
                session_key=session_key,
                channel="cli",
                chat_id="setup-user-actions",
                on_progress=_progress,
                system_prompt=system_prompt,
            )
        _thinking = None
        _print_agent_response(response, render_markdown=True)

        # Interactive dialogue loop
        while True:
            try:
                _flush_pending_tty_input()
                user_input = await _read_interactive_input_async()
                command = user_input.strip()
                if not command:
                    continue
                if _is_exit_command(command):
                    break

                _thinking = _ThinkingSpinner(enabled=True)
                with _thinking:
                    response = await agent_loop.process_direct(
                        user_input,
                        session_key=session_key,
                        channel="cli",
                        chat_id="setup-user-actions",
                        on_progress=_progress,
                        system_prompt=system_prompt,
                    )
                _thinking = None
                _print_agent_response(response, render_markdown=True)
            except (KeyboardInterrupt, EOFError):
                break

        await agent_loop.close_mcp()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

    _restore_terminal()
    console.print("\n[green]\u2713[/green] Setup session complete.")


@app.command(name="setup-user-actions")
def setup_user_actions(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Interactive setup session for user actions and workflows.

    If saved instructions exist from a prior install, they are used automatically.
    Otherwise, paste instructions describing actions or automations you want,
    then work through an interactive dialogue with the agent to set them up.
    """
    from hazel.config.paths import get_pending_setup_user_actions_path

    cfg = _load_runtime_config(config, workspace)

    if not cfg.get_api_key():
        console.print("[red]Error: No LLM provider is configured.[/red]")
        console.print("Set up a provider first with [cyan]hazel quickstart[/cyan] or [cyan]hazel onboard --wizard[/cyan]")
        raise typer.Exit(1)

    # Check for saved pending instructions from install
    pending_path = get_pending_setup_user_actions_path()
    if pending_path.exists():
        instructions = pending_path.read_text(encoding="utf-8").strip()
        if instructions:
            console.print("[green]✓[/green] Found saved user actions instructions from install.")
            _run_setup_user_actions(cfg, instructions)
            pending_path.unlink()
            return

    console.print("Paste your setup instructions below and press [bold]Enter[/bold]:\n")
    try:
        import questionary
        instructions = questionary.text("Instructions:", qmark=">", multiline=False).ask()
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        raise typer.Exit()
    if instructions is None:
        raise typer.Exit()

    if not instructions.strip():
        console.print("[yellow]No instructions provided.[/yellow]")
        raise typer.Exit()

    _run_setup_user_actions(cfg, instructions)


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    import json

    from hazel.channels.registry import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _setup_dashboard(config: Config) -> None:
    """Set up the Canvas dashboard as a systemd user service."""
    import shutil
    import subprocess

    dashboard_cfg = config.gateway.dashboard
    if not dashboard_cfg.enabled:
        console.print("[dim]Dashboard disabled in config, skipping.[/dim]")
        return

    # Check Node.js is available
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        console.print("[yellow]![/yellow] Node.js not found — skipping dashboard setup.")
        console.print("  To enable the dashboard later:")
        console.print("    1. Install Node.js: [cyan]curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash - && sudo apt-get install -y nodejs[/cyan]")
        console.print("    2. Re-run: [cyan]hazel onboard[/cyan]  (your config will be preserved)")
        return

    # Resolve canvas source dir (bundled in package at hazel/canvas/)
    from importlib.resources import files as pkg_files
    try:
        canvas_src = pkg_files("hazel") / "canvas"
    except Exception:
        canvas_src = None

    # Fallback: check for canvas/ next to the hazel package (dev installs)
    if not canvas_src or not canvas_src.is_dir():
        dev_canvas = Path(__file__).resolve().parent.parent.parent / "canvas"
        if dev_canvas.is_dir():
            canvas_src = dev_canvas
        else:
            console.print("[yellow]![/yellow] Canvas files not found — skipping dashboard setup.")
            return

    # Copy source files to ~/.hazel/canvas/
    canvas_dest = Path.home() / ".hazel" / "canvas"
    canvas_dest.mkdir(parents=True, exist_ok=True)
    for fname in ("dashboard-server.js", "dashboard.html", "package.json"):
        src_file = canvas_src / fname
        if src_file.is_file():
            (canvas_dest / fname).write_text(
                src_file.read_text(encoding="utf-8"), encoding="utf-8"
            )
    console.print(f"[green]\u2713[/green] Canvas files copied to {canvas_dest}")

    # npm install (skip if node_modules already exists and package.json unchanged)
    node_modules = canvas_dest / "node_modules"
    if not node_modules.exists():
        console.print("[dim]Installing dashboard dependencies...[/dim]")
        result = subprocess.run(
            [npm, "install", "--production", "--no-audit", "--no-fund"],
            cwd=str(canvas_dest),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            console.print(f"[yellow]![/yellow] npm install failed: {result.stderr.strip()[:200]}")
            return
        console.print("[green]\u2713[/green] Dashboard dependencies installed")

    # Generate dashboard auth secret if it doesn't exist
    from hazel.agent.tools.dashboard import get_or_create_secret
    get_or_create_secret()
    console.print("[green]\u2713[/green] Dashboard auth secret ready")

    # Set up and start the dashboard as a background service
    port = dashboard_cfg.port
    host = dashboard_cfg.host
    manual_cmd = f"DASHBOARD_HOST={host} DASHBOARD_PORT={port} node {canvas_dest / 'dashboard-server.js'}"

    if sys.platform == "darwin":
        _dashboard_service_macos(node, canvas_dest, host, port, manual_cmd)
    else:
        _dashboard_service_linux(node, canvas_dest, host, port, manual_cmd)


def _dashboard_service_macos(node: str, canvas_dest: Path, host: str, port: int, manual_cmd: str) -> None:
    """Install and start the dashboard as a macOS LaunchAgent."""
    import plistlib
    import subprocess

    label = "ai.hazel.dashboard"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{label}.plist"

    plist = {
        "Label": label,
        "ProgramArguments": [node, str(canvas_dest / "dashboard-server.js")],
        "EnvironmentVariables": {
            "NODE_ENV": "production",
            "DASHBOARD_PORT": str(port),
            "DASHBOARD_HOST": host,
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(Path.home() / ".hazel" / "logs" / "dashboard.out.log"),
        "StandardErrorPath": str(Path.home() / ".hazel" / "logs" / "dashboard.err.log"),
    }
    (Path.home() / ".hazel" / "logs").mkdir(parents=True, exist_ok=True)

    # Unload existing service before overwriting plist
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True, timeout=10,
        )

    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    try:
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 and "already bootstrapped" not in result.stderr.lower():
            console.print(f"[yellow]![/yellow] launchctl bootstrap failed: {result.stderr.strip()[:200]}")
            console.print(f"  Start manually: [cyan]{manual_cmd}[/cyan]")
            return

        console.print(f"[green]\u2713[/green] Dashboard running at [cyan]http://{host}:{port}[/cyan]")
        console.print(f"  [dim]Logs:[/dim]    ~/.hazel/logs/dashboard.*.log")
        console.print(f"  [dim]Stop:[/dim]    launchctl bootout gui/{os.getuid()} {plist_path}")
    except Exception as e:
        console.print(f"[yellow]![/yellow] Could not start service: {e}")
        console.print(f"  Start manually: [cyan]{manual_cmd}[/cyan]")


def _dashboard_service_linux(node: str, canvas_dest: Path, host: str, port: int, manual_cmd: str) -> None:
    """Install and start the dashboard as a systemd user service."""
    import subprocess

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    service_path = systemd_dir / "hazel-dashboard.service"

    service_content = f"""[Unit]
Description=Hazel Canvas Dashboard
After=default.target

[Service]
Type=simple
ExecStart={node} {canvas_dest / 'dashboard-server.js'}
Restart=always
RestartSec=5
Environment=NODE_ENV=production
Environment=DASHBOARD_PORT={port}
Environment=DASHBOARD_HOST={host}

[Install]
WantedBy=default.target
"""
    service_path.write_text(service_content, encoding="utf-8")

    # Enable user lingering (required for user services to survive logout, especially on Pi)
    try:
        subprocess.run(["loginctl", "enable-linger"], capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Enable and start the service
    try:
        reload = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, timeout=15,
        )
        if reload.returncode != 0:
            console.print(f"[yellow]![/yellow] systemctl --user daemon-reload failed: {reload.stderr.strip()[:200]}")
            console.print(f"  Start manually: [cyan]{manual_cmd}[/cyan]")
            return

        enable = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "hazel-dashboard"],
            capture_output=True, text=True, timeout=15,
        )
        if enable.returncode != 0:
            console.print(f"[yellow]![/yellow] Could not enable dashboard service: {enable.stderr.strip()[:200]}")
            console.print(f"  Start manually: [cyan]{manual_cmd}[/cyan]")
            return

        # Verify it started
        check = subprocess.run(
            ["systemctl", "--user", "is-active", "hazel-dashboard"],
            capture_output=True, text=True, timeout=10,
        )
        if check.stdout.strip() == "active":
            console.print(f"[green]\u2713[/green] Dashboard running at [cyan]http://{host}:{port}[/cyan]")
        else:
            console.print(f"[yellow]![/yellow] Service installed but not active. Check with: systemctl --user status hazel-dashboard")
    except FileNotFoundError:
        console.print("[yellow]![/yellow] systemctl not found — start the dashboard manually:")
        console.print(f"  [cyan]{manual_cmd}[/cyan]")
    except Exception as e:
        console.print(f"[yellow]![/yellow] Could not start service: {e}")
        console.print(f"  Start manually: [cyan]{manual_cmd}[/cyan]")


def _make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    from hazel.providers.azure_openai_provider import AzureOpenAIProvider
    from hazel.providers.base import GenerationSettings
    from hazel.providers.openai_codex_provider import OpenAICodexProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    # OpenAI Codex (OAuth)
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        provider = OpenAICodexProvider(default_model=model)
    # Custom: direct OpenAI-compatible endpoint, bypasses LiteLLM
    elif provider_name == "custom":
        from hazel.providers.custom_provider import CustomProvider
        provider = CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    # Azure OpenAI: direct Azure OpenAI endpoint with deployment name
    elif provider_name == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            console.print("Set them in ~/.hazel/config.json under providers.azure_openai section")
            console.print("Use the model field to specify the deployment name.")
            raise typer.Exit(1)
        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    else:
        from hazel.providers.litellm_provider import LiteLLMProvider
        from hazel.providers.registry import find_by_name
        spec = find_by_name(provider_name)
        if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and (spec.is_oauth or spec.is_local)):
            console.print("[red]Error: No API key configured.[/red]")
            console.print("Set one in ~/.hazel/config.json under providers section")
            raise typer.Exit(1)
        provider = LiteLLMProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            provider_name=provider_name,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from hazel.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file."""
    import json
    from hazel.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )



# ============================================================================
# ---------------------------------------------------------------------------
# System-event handlers (pure-code cron jobs, no LLM)
# ---------------------------------------------------------------------------


async def _handle_system_event(
    job: "CronJob",
    workspace: Path,
    bus: "MessageBus",
) -> str | None:
    """Dispatch a system_event cron job to its pure-code handler."""
    from hazel.cron.intent_notifier import check_and_notify
    from hazel.cron.service import INTENT_NOTIFICATIONS_NAME

    if job.name == INTENT_NOTIFICATIONS_NAME:
        channel = job.payload.channel or "cli"
        chat_id = job.payload.to or "direct"
        return await check_and_notify(workspace, channel, chat_id, bus)

    from loguru import logger
    logger.warning("Unknown system_event job: {}", job.name)
    return None


# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    foreground: bool = typer.Option(False, "--foreground", "--fg", help="Run in the foreground instead of as a background service"),
    stop: bool = typer.Option(False, "--stop", help="Stop the background gateway service"),
):
    """Start the Hazel gateway (runs as a background service by default)."""
    if stop:
        _gateway_service_stop()
        return

    if not foreground:
        _gateway_service_install(config, port)
        return

    from hazel.agent.loop import AgentLoop
    from hazel.bus.queue import MessageBus
    from hazel.channels.manager import ChannelManager
    from hazel.config.paths import get_cron_dir
    from hazel.cron.service import CronService
    from hazel.cron.types import CronJob
    from hazel.heartbeat.service import HeartbeatService
    from hazel.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config, workspace)
    port = port if port is not None else config.gateway.port

    console.print(f"{__logo__} Starting Hazel gateway version {__version__} on port {port}...")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)
    cron.bootstrap_default_jobs(channels_config=config.channels)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        dashboard_config=config.gateway.dashboard,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent (or pure-code for system_event)."""
        # System events are handled by pure-code handlers — no LLM needed.
        if job.payload.kind == "system_event":
            return await _handle_system_event(job, config.workspace_path, bus)

        from hazel.agent.tools.cron import CronTool
        from hazel.agent.tools.message import MessageTool
        from hazel.utils.evaluator import evaluate_response

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            should_notify = await evaluate_response(
                response, job.payload.message, provider, agent.model,
            )
            if should_notify:
                from hazel.bus.events import OutboundMessage
                await bus.publish_outbound(OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response,
                ))
        return response
    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from hazel.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback
            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


def _gateway_service_install(config_path: str | None, port: int | None) -> None:
    """Install and start the gateway as a background service."""
    import shutil
    import subprocess

    hazel_bin = shutil.which("hazel")
    if not hazel_bin:
        console.print("[red]ERROR:[/red] 'hazel' not found on PATH.")
        return

    exec_parts = [hazel_bin, "gateway", "--foreground"]
    if config_path:
        exec_parts.extend(["--config", config_path])
    if port is not None:
        exec_parts.extend(["--port", str(port)])

    if sys.platform == "darwin":
        _gateway_service_install_macos(exec_parts)
    else:
        _gateway_service_install_linux(exec_parts)


def _gateway_service_install_macos(exec_parts: list[str]) -> None:
    """Install and start the gateway as a macOS LaunchAgent."""
    import plistlib
    import subprocess

    label = "ai.hazel.gateway"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{label}.plist"

    plist = {
        "Label": label,
        "ProgramArguments": exec_parts,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(Path.home() / ".hazel" / "logs" / "gateway.out.log"),
        "StandardErrorPath": str(Path.home() / ".hazel" / "logs" / "gateway.err.log"),
    }
    (Path.home() / ".hazel" / "logs").mkdir(parents=True, exist_ok=True)

    # Unload existing service before overwriting plist
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True, timeout=10,
        )

    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    try:
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 and "already bootstrapped" not in result.stderr.lower():
            console.print(f"[yellow]![/yellow] launchctl bootstrap failed: {result.stderr.strip()[:200]}")
            console.print(f"  Run manually: [cyan]{' '.join(exec_parts)}[/cyan]")
            return

        console.print("[green]\u2713[/green] Gateway is running in the background.")
        console.print(f"  [dim]Logs:[/dim]    ~/.hazel/logs/gateway.*.log")
        console.print(f"  [dim]Stop:[/dim]    hazel gateway --stop")
    except Exception as e:
        console.print(f"[yellow]![/yellow] Could not start service: {e}")
        console.print(f"  Run manually: [cyan]{' '.join(exec_parts)}[/cyan]")


def _gateway_service_install_linux(exec_parts: list[str]) -> None:
    """Install and start the gateway as a systemd user service."""
    import subprocess

    exec_start = " ".join(exec_parts)

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    service_path = systemd_dir / "hazel-gateway.service"

    service_content = f"""[Unit]
Description=Hazel Gateway
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=%h

[Install]
WantedBy=default.target
"""
    service_path.write_text(service_content, encoding="utf-8")

    # Enable user lingering (required for user services to survive logout, especially on Pi)
    try:
        subprocess.run(["loginctl", "enable-linger"], capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        reload = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, timeout=15,
        )
        if reload.returncode != 0:
            console.print(f"[yellow]![/yellow] systemctl --user daemon-reload failed: {reload.stderr.strip()[:200]}")
            console.print(f"  Run manually: [cyan]{exec_start}[/cyan]")
            return

        enable = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "hazel-gateway"],
            capture_output=True, text=True, timeout=15,
        )
        if enable.returncode != 0:
            console.print(f"[yellow]![/yellow] Could not enable gateway service: {enable.stderr.strip()[:200]}")
            console.print(f"  Run manually: [cyan]{exec_start}[/cyan]")
            return

        check = subprocess.run(
            ["systemctl", "--user", "is-active", "hazel-gateway"],
            capture_output=True, text=True, timeout=10,
        )
        if check.stdout.strip() == "active":
            console.print("[green]\u2713[/green] Gateway is running in the background.")
            console.print("  [dim]Status:[/dim]   systemctl --user status hazel-gateway")
            console.print("  [dim]Logs:[/dim]     journalctl --user -u hazel-gateway -f")
            console.print("  [dim]Stop:[/dim]     hazel gateway --stop")
            console.print("  [dim]Restart:[/dim]  systemctl --user restart hazel-gateway")
        else:
            console.print("[yellow]![/yellow] Service installed but not active.")
            console.print("  Check: [cyan]systemctl --user status hazel-gateway[/cyan]")
    except FileNotFoundError:
        console.print("[yellow]![/yellow] systemctl not found — cannot run as daemon on this system.")
        console.print("  Use [cyan]nohup hazel gateway &[/cyan] or run inside tmux/screen instead.")
    except Exception as e:
        console.print(f"[yellow]![/yellow] Could not start service: {e}")


def _gateway_service_stop() -> None:
    """Stop and disable the gateway background service."""
    import subprocess

    if sys.platform == "darwin":
        label = "ai.hazel.gateway"
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        try:
            result = subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 or "not found" in result.stderr.lower():
                console.print("[green]\u2713[/green] Gateway service stopped.")
            else:
                console.print(f"[yellow]![/yellow] launchctl bootout: {result.stderr.strip()[:200]}")
            if plist_path.exists():
                plist_path.unlink()
        except Exception as e:
            console.print(f"[red]ERROR:[/red] {e}")
    else:
        try:
            subprocess.run(["systemctl", "--user", "stop", "hazel-gateway"], capture_output=True, timeout=15)
            subprocess.run(["systemctl", "--user", "disable", "hazel-gateway"], capture_output=True, timeout=15)
            console.print("[green]\u2713[/green] Gateway service stopped and disabled.")
        except FileNotFoundError:
            console.print("[yellow]![/yellow] systemctl not found.")
        except Exception as e:
            console.print(f"[red]ERROR:[/red] {e}")



# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show Hazel runtime logs during chat"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from hazel.agent.loop import AgentLoop
    from hazel.bus.queue import MessageBus
    from hazel.config.paths import get_cron_dir
    from hazel.cron.service import CronService

    config = _load_runtime_config(config, workspace)
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("hazel")
    else:
        logger.disable("hazel")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        dashboard_config=config.gateway.dashboard,
    )

    # Shared reference for progress callbacks
    _thinking: _ThinkingSpinner | None = None

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        _print_cli_progress_line(content, _thinking)

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            nonlocal _thinking
            _thinking = _ThinkingSpinner(enabled=not logs)
            with _thinking:
                response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
            _thinking = None
            _print_agent_response(response, render_markdown=markdown)
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from hazel.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                await _print_interactive_progress_line(msg.content, _thinking)

                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            await _print_interactive_response(msg.content, render_markdown=markdown)

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                        ))

                        nonlocal _thinking
                        _thinking = _ThinkingSpinner(enabled=not logs)
                        with _thinking:
                            await turn_done.wait()
                        _thinking = None

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from hazel.channels.registry import discover_all
    from hazel.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    from hazel.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    npm_path = shutil.which("npm")
    if not npm_path:
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # hazel/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall hazel-ai")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run([npm_path, "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run([npm_path, "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import shutil
    import subprocess

    from hazel.config.loader import load_config
    from hazel.config.paths import get_runtime_subdir

    config = load_config()
    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    env = {**os.environ}
    wa_cfg = getattr(config.channels, "whatsapp", None) or {}
    bridge_token = wa_cfg.get("bridgeToken", "") if isinstance(wa_cfg, dict) else getattr(wa_cfg, "bridge_token", "")
    if bridge_token:
        env["BRIDGE_TOKEN"] = bridge_token
    env["AUTH_DIR"] = str(get_runtime_subdir("whatsapp-auth"))

    npm_path = shutil.which("npm")
    if not npm_path:
        console.print("[red]npm not found. Please install Node.js.[/red]")
        raise typer.Exit(1)

    try:
        subprocess.run([npm_path, "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")


# ============================================================================
# Plugin Commands
# ============================================================================

plugins_app = typer.Typer(help="Manage channel plugins")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """List all discovered channels (built-in and plugins)."""
    from hazel.channels.registry import discover_all, discover_channel_names
    from hazel.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    table = Table(title="Channel Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled", style="green")

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            source,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show Hazel status."""
    from hazel.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} Hazel Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from hazel.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


@app.command()
def update(
    version: str | None = typer.Option(None, "--version", "-V", help="Install a specific version (e.g. v0.1.5)"),
):
    """Update Hazel to the latest version."""
    import subprocess
    import shutil

    uv = shutil.which("uv")
    if not uv:
        console.print("[red]ERROR:[/red] uv not found. Reinstall with:")
        console.print("  [cyan]curl -LsSf https://raw.githubusercontent.com/ThomasPinella/hazel/main/scripts/install.sh | bash[/cyan]")
        raise typer.Exit(1)

    console.print(f"{__logo__} Updating Hazel...\n")
    console.print(f"[dim]Current version: {__version__}[/dim]")

    # Fetch the wheel URL from GitHub releases
    import urllib.request
    import json

    repo = "ThomasPinella/hazel"
    if version:
        tag = version if version.startswith("v") else f"v{version}"
        api_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    else:
        api_url = f"https://api.github.com/repos/{repo}/releases/latest"

    try:
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            release = json.loads(resp.read())
    except Exception as e:
        console.print(f"[red]ERROR:[/red] Could not fetch release: {e}")
        raise typer.Exit(1)

    wheel_url = None
    for asset in release.get("assets", []):
        if asset["name"].endswith(".whl"):
            wheel_url = asset["browser_download_url"]
            break

    if not wheel_url:
        console.print("[red]ERROR:[/red] No .whl found in release assets.")
        raise typer.Exit(1)

    release_tag = release.get("tag_name", "unknown")
    console.print(f"[dim]Latest version: {release_tag}[/dim]\n")

    # Clean up any broken leftover environment (uv chokes on corrupt venvs)
    tool_env = Path.home() / ".local" / "share" / "uv" / "tools" / "hazel-ai"
    if tool_env.is_dir():
        import shutil as _shutil
        _shutil.rmtree(tool_env, ignore_errors=True)

    # Upgrade in-place (don't uninstall first — if reinstall fails, we'd lose the binary)
    result = subprocess.run(
        [uv, "tool", "install", "--force", f"hazel-ai @ {wheel_url}"],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        console.print(f"[red]ERROR:[/red] {result.stderr.strip()}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Updated to {release_tag}")

    # Restart gateway service if running
    try:
        check = subprocess.run(
            ["systemctl", "--user", "is-active", "hazel-gateway"],
            capture_output=True, text=True, timeout=5,
        )
        if check.stdout.strip() == "active":
            subprocess.run(["systemctl", "--user", "restart", "hazel-gateway"], capture_output=True, timeout=15)
            console.print("[green]✓[/green] Gateway service restarted")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from hazel.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
