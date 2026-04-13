"""Quickstart wizard — get Hazel running in under 2 minutes."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from hazel.config.schema import Config

console = Console()

# ── Minimax defaults ────────────────────────────────────────────────────────

_DEFAULT_MODEL = "minimax/minimax-m2.7"
_DEFAULT_MAX_TOKENS = 130_000
_DEFAULT_CONTEXT_WINDOW = 204_800
_DEFAULT_MAX_TOOL_ITERATIONS = 100
_DEFAULT_PROVIDER = "minimax"


def _get_questionary():
    """Return questionary or raise a clear error."""
    try:
        import questionary
    except ModuleNotFoundError:
        raise RuntimeError(
            "Quickstart requires the 'questionary' dependency. "
            "Install with: pip install hazel-ai[wizard]"
        )
    return questionary


# ── Step 1: LLM Provider ───────────────────────────────────────────────────


def _step_provider(config: Config) -> bool:
    """Configure the LLM provider. Returns False if the user cancelled."""
    q = _get_questionary()

    console.print()
    console.print(
        Panel(
            "[bold]Step 1 of 2 — LLM Provider[/bold]\n\n"
            f"By default, Hazel uses [cyan]MiniMax (minimax-m2.7)[/cyan] — a powerful\n"
            "and affordable model with a 200k context window.\n\n"
            "You just need a MiniMax API key to get started.",
            border_style="blue",
        )
    )

    console.print()
    console.print("[bold]How to get your MiniMax API key:[/bold]")
    console.print("  1. Go to [cyan]https://www.minimaxi.com[/cyan]")
    console.print("  2. Sign up or log in")
    console.print("  3. Navigate to API Keys and create a new key")
    console.print("  4. [yellow]Important:[/yellow] Add funds to your account for the API to work")
    console.print()

    choice = q.select(
        "How would you like to set up your LLM?",
        choices=[
            "Enter MiniMax API key (recommended)",
            "Advanced — choose a different provider",
        ],
        default="Enter MiniMax API key (recommended)",
        qmark=">",
    ).ask()

    if choice is None:
        return False

    if "Advanced" in choice:
        return _step_provider_advanced(config)

    # Simple path: just get the API key
    api_key = q.password("MiniMax API key:", qmark=">").ask()
    if api_key is None or api_key.strip() == "":
        console.print("[yellow]No API key entered. You can add it later in your config.[/yellow]")
    else:
        config.providers.minimax.api_key = api_key.strip()

    # Set Minimax defaults
    config.agents.defaults.model = _DEFAULT_MODEL
    config.agents.defaults.provider = _DEFAULT_PROVIDER
    config.agents.defaults.max_tokens = _DEFAULT_MAX_TOKENS
    config.agents.defaults.context_window_tokens = _DEFAULT_CONTEXT_WINDOW
    config.agents.defaults.max_tool_iterations = _DEFAULT_MAX_TOOL_ITERATIONS

    console.print("[green]✓[/green] LLM provider configured")
    return True


def _step_provider_advanced(config: Config) -> bool:
    """Fall back to the full wizard for provider + agent config."""
    from hazel.cli.onboard_wizard import _configure_general_settings, _configure_providers

    console.print()
    console.print("[dim]Opening full provider setup...[/dim]")
    _configure_providers(config)
    _configure_general_settings(config, "Agent Settings")
    return True


# ── Step 2: Channel (Telegram) ─────────────────────────────────────────────


def _step_channel(config: Config) -> bool:
    """Configure a chat channel. Returns False if the user cancelled."""
    q = _get_questionary()

    console.print()
    console.print(
        Panel(
            "[bold]Step 2 of 2 — Chat Channel[/bold]\n\n"
            "Hazel connects to chat apps so you can talk to your AI\n"
            "from your phone. [cyan]Telegram[/cyan] is the easiest to set up.",
            border_style="blue",
        )
    )

    console.print()
    console.print("[bold]How to create a Telegram bot:[/bold]")
    console.print("  1. Open Telegram and search for [cyan]@BotFather[/cyan]")
    console.print("  2. Send [cyan]/newbot[/cyan] and follow the prompts")
    console.print("  3. BotFather will give you a bot token — copy it")
    console.print()

    choice = q.select(
        "How would you like to set up your chat channel?",
        choices=[
            "Enter Telegram bot token (recommended)",
            "Advanced — choose a different channel",
            "Skip — I'll set this up later",
        ],
        default="Enter Telegram bot token (recommended)",
        qmark=">",
    ).ask()

    if choice is None:
        return False

    if "Advanced" in choice:
        return _step_channel_advanced(config)

    if "Skip" in choice:
        console.print("[dim]Channel setup skipped. You can configure it later with: hazel onboard --wizard[/dim]")
        return True

    # Simple path: just get the bot token
    bot_token = q.password("Telegram bot token:", qmark=">").ask()
    if bot_token is None or bot_token.strip() == "":
        console.print("[yellow]No token entered. You can add it later in your config.[/yellow]")
        return True

    # Get the current telegram config dict (or create one)
    telegram_cfg = getattr(config.channels, "telegram", None)
    if telegram_cfg is None:
        telegram_cfg = {}
    elif not isinstance(telegram_cfg, dict):
        telegram_cfg = {}

    telegram_cfg["token"] = bot_token.strip()
    telegram_cfg["enabled"] = True
    setattr(config.channels, "telegram", telegram_cfg)

    console.print("[green]✓[/green] Telegram channel configured")

    # Optionally set allowed users
    console.print()
    console.print("[dim]Tip: You can restrict who can talk to your bot by adding Telegram[/dim]")
    console.print("[dim]usernames to the allow list. Leave blank to allow everyone.[/dim]")

    allow_from = q.text(
        "Allowed usernames (comma-separated, or press Enter to skip):",
        default="",
        qmark=">",
    ).ask()

    if allow_from and allow_from.strip():
        usernames = [u.strip().lstrip("@") for u in allow_from.split(",") if u.strip()]
        if usernames:
            telegram_cfg["allowFrom"] = usernames
            setattr(config.channels, "telegram", telegram_cfg)
            console.print(f"[green]✓[/green] Allow list set: {', '.join(usernames)}")

    return True


def _step_channel_advanced(config: Config) -> bool:
    """Fall back to the full wizard for channel config."""
    from hazel.cli.onboard_wizard import _configure_channels

    console.print()
    console.print("[dim]Opening full channel setup...[/dim]")
    _configure_channels(config)
    return True


# ── Main entry point ───────────────────────────────────────────────────────


def run_quickstart(config: Config) -> tuple[Config, bool]:
    """Run the quickstart wizard.

    Returns (config, should_save) tuple.
    """
    from hazel import __logo__, __version__

    console.print()
    console.print(
        Panel(
            f"{__logo__} [bold cyan]Hazel Quickstart[/bold cyan]  [dim]v{__version__}[/dim]\n\n"
            "Let's get you up and running in just two steps:\n"
            "  1. Set up your LLM provider (AI brain)\n"
            "  2. Connect a chat channel (Telegram)",
            border_style="green",
        )
    )

    # Step 1: Provider
    if not _step_provider(config):
        console.print("[yellow]Setup cancelled.[/yellow]")
        return config, False

    # Step 2: Channel
    if not _step_channel(config):
        console.print("[yellow]Setup cancelled.[/yellow]")
        return config, False

    # Done!
    console.print()
    console.print(
        Panel(
            "[bold green]Setup complete![/bold green]\n\n"
            "Want to customize more settings? Run:\n"
            "  [cyan]hazel onboard --wizard[/cyan]",
            border_style="green",
        )
    )

    return config, True
