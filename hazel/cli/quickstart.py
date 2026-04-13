"""Quickstart wizard — get Hazel running in under 2 minutes."""

from __future__ import annotations

import sys
from getpass import getpass

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

# ── Terminal-safe input helpers ─────────────────────────────────────────────
# prompt_toolkit (used by questionary) calls termios.tcsetattr() to enter raw
# mode.  When stdin is a /dev/tty redirect inside a `curl | bash` pipe, that
# syscall can fail with EINVAL.  We detect this upfront and fall back to plain
# input()/getpass() so the install flow doesn't crash.

_USE_FALLBACK: bool | None = None  # lazy-init


def _should_use_fallback() -> bool:
    """Return True if prompt_toolkit terminal ops will fail."""
    global _USE_FALLBACK
    if _USE_FALLBACK is not None:
        return _USE_FALLBACK
    try:
        import termios

        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        # tcgetattr can succeed while tcsetattr fails with EINVAL
        # (e.g. /dev/tty redirect inside curl|bash).  Test the actual
        # write operation that prompt_toolkit needs.
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        _USE_FALLBACK = False
    except Exception:
        _USE_FALLBACK = True
    return _USE_FALLBACK


# ── Fallback prompt objects (same call pattern as questionary) ──────────────


class _FallbackResult:
    """Wraps a callable so `.ask()` mirrors questionary's API."""

    def __init__(self, fn, *args, **kwargs):
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def ask(self):
        return self._fn(*self._args, **self._kwargs)


class _FallbackInput:
    """Drop-in replacement for questionary when prompt_toolkit is broken."""

    def select(self, message, choices, default=None, **_kw):
        def _ask():
            console.print(f"[bold]{message}[/bold]")
            for i, c in enumerate(choices, 1):
                marker = " [cyan](default)[/cyan]" if c == default else ""
                console.print(f"  {i}. {c}{marker}")
            while True:
                raw = input(f"Enter choice [1-{len(choices)}] (default 1): ").strip()
                if raw == "":
                    return choices[0] if default is None else default
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(choices):
                        return choices[idx - 1]
                except ValueError:
                    pass
                console.print(f"[yellow]Please enter a number between 1 and {len(choices)}[/yellow]")

        return _FallbackResult(_ask)

    def password(self, message, **_kw):
        return _FallbackResult(lambda: getpass(f"{message} "))

    def text(self, message, default="", **_kw):
        def _ask():
            suffix = f" [{default}]" if default else ""
            raw = input(f"{message}{suffix} ").strip()
            return raw if raw else default

        return _FallbackResult(_ask)

    def confirm(self, message, default=False, **_kw):
        def _ask():
            hint = "Y/n" if default else "y/N"
            raw = input(f"{message} ({hint}): ").strip().lower()
            if raw == "":
                return default
            return raw in ("y", "yes")

        return _FallbackResult(_ask)


def _get_questionary():
    """Return questionary or a plain-input fallback if the terminal can't
    support prompt_toolkit's raw-mode operations."""
    if _should_use_fallback():
        return _FallbackInput()
    try:
        import questionary

        # Smoke-test: make sure prompt_toolkit can actually init the terminal.
        # Some environments pass isatty() but fail on tcsetattr().
        from prompt_toolkit.input import create_input

        inp = create_input()
        inp.fileno()  # forces FD resolution — raises on bad terminals
        return questionary
    except Exception:
        return _FallbackInput()


# ── Step 1: LLM Provider ───────────────────────────────────────────────────


def _step_provider(config: Config, total_steps: int = 2) -> bool:
    """Configure the LLM provider. Returns False if the user cancelled."""
    q = _get_questionary()

    while True:
        console.print()
        console.print(
            Panel(
                f"[bold]Step 1 of {total_steps} — LLM Provider[/bold]\n\n"
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
            _step_provider_advanced(config)
            # Loop back to the provider choice so user can pick MiniMax or go advanced again
            continue

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


def _step_provider_advanced(config: Config) -> None:
    """Fall back to the full wizard for provider + agent config."""
    from hazel.cli.onboard_wizard import _configure_general_settings, _configure_providers

    console.print()
    console.print("[dim]Opening full provider setup...[/dim]")
    _configure_providers(config)
    _configure_general_settings(config, "Agent Settings")


# ── Step 2: Channel (Telegram) ─────────────────────────────────────────────


_GO_BACK = "go_back"


def _print_qr(url: str, indent: str = "  ") -> None:
    """Print a compact Unicode QR code for *url* to the terminal."""
    try:
        import qrcode
    except ModuleNotFoundError:
        return  # silently skip — dep missing, not worth crashing over

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    # Render two rows per line using half-block Unicode chars for a compact
    # QR that scans reliably in most terminals.
    for y in range(0, len(matrix), 2):
        line = indent
        for x in range(len(matrix[y])):
            top = matrix[y][x]
            bottom = matrix[y + 1][x] if y + 1 < len(matrix) else False
            if top and bottom:
                line += "\u2588"  # full block
            elif top:
                line += "\u2580"  # upper half
            elif bottom:
                line += "\u2584"  # lower half
            else:
                line += " "
        # Use print() directly so rich doesn't mangle the unicode blocks
        print(line)


def _step_channel(config: Config, total_steps: int = 2) -> bool | str:
    """Configure a chat channel.

    Returns True on success, False if cancelled, or _GO_BACK to return to step 1.
    """
    q = _get_questionary()

    console.print()
    console.print(
        Panel(
            f"[bold]Step 2 of {total_steps} — Chat Channel[/bold]\n\n"
            "Hazel connects to chat apps so you can talk to your AI\n"
            "from your phone. [cyan]Telegram[/cyan] is the easiest to set up.",
            border_style="blue",
        )
    )

    # ── BotFather instructions ─────────────────────────────────────────
    botfather_url = "https://telegram.me/BotFather"
    console.print()
    console.print("[bold]Step 2a — Create your Telegram bot[/bold]")
    console.print()
    console.print(
        f"  Open BotFather: [link={botfather_url}][cyan]{botfather_url}[/cyan][/link]"
    )
    console.print("  (or scan this QR code on your phone)")
    console.print()
    _print_qr(botfather_url)
    console.print()
    console.print("  [bold]What to do:[/bold]")
    console.print("    1. Send [cyan]/newbot[/cyan] to BotFather")
    console.print("    2. Follow its prompts (pick a name + username)")
    console.print("    3. BotFather will reply with a [bold]bot token[/bold] — copy it")
    console.print("    4. Paste the token below")
    console.print()

    choice = q.select(
        "How would you like to set up your chat channel?",
        choices=[
            "Enter Telegram bot token (recommended)",
            "Advanced — choose a different channel",
            "Skip — I'll set this up later",
            "<- Back to LLM provider setup",
        ],
        default="Enter Telegram bot token (recommended)",
        qmark=">",
    ).ask()

    if choice is None:
        return False

    if "<- Back" in choice:
        return _GO_BACK

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

    console.print("[green]✓[/green] Bot token saved")

    # ── userinfobot instructions (lock bot to just the user) ──────────
    userinfo_url = "https://telegram.me/userinfobot"
    console.print()
    console.print("[bold]Step 2b — Lock your bot to just you[/bold]")
    console.print()
    console.print(
        "  To make sure [bold]only you[/bold] can talk to your bot, we need your"
    )
    console.print("  Telegram user ID.")
    console.print()
    console.print(
        f"  Open userinfobot: [link={userinfo_url}][cyan]{userinfo_url}[/cyan][/link]"
    )
    console.print("  (or scan this QR code on your phone)")
    console.print()
    _print_qr(userinfo_url)
    console.print()
    console.print("  [bold]What to do:[/bold]")
    console.print("    1. Send [cyan]/start[/cyan] to @userinfobot")
    console.print("    2. It will reply with your numeric [bold]user ID[/bold] (e.g. 123456789)")
    console.print("    3. Paste it below")
    console.print()
    console.print(
        "  [dim]Or press Enter to skip and allow everyone (not recommended).[/dim]"
    )

    user_id = q.text(
        "Your Telegram user ID:",
        default="",
        qmark=">",
    ).ask()

    if user_id and user_id.strip():
        uid = user_id.strip()
        telegram_cfg["allowFrom"] = [uid]
        setattr(config.channels, "telegram", telegram_cfg)
        console.print(f"[green]✓[/green] Bot locked to user ID: {uid}")
    else:
        console.print("[yellow]![/yellow] No user ID — bot will accept messages from anyone.")
        console.print(
            "  [dim]To lock it later, edit allowFrom in your config.[/dim]"
        )

    return True


def _step_channel_advanced(config: Config) -> bool:
    """Fall back to the full wizard for channel config."""
    from hazel.cli.onboard_wizard import _configure_channels

    console.print()
    console.print("[dim]Opening full channel setup...[/dim]")
    _configure_channels(config)
    return True


# ── Main entry point ───────────────────────────────────────────────────────


def _save_pending_instructions(path, instructions: str) -> None:
    """Save setup instructions to a pending file for later use."""
    from pathlib import Path

    Path(path).write_text(instructions, encoding="utf-8")


def _clear_pending_instructions(path) -> None:
    """Remove a pending setup instructions file after successful execution."""
    from pathlib import Path

    p = Path(path)
    if p.exists():
        p.unlink()


def _step_setup_skills(config: Config, auto_instructions: str | None = None,
                       total_steps: int = 5) -> None:
    """Run skills setup instructions through the agent.

    If *auto_instructions* is provided (from a setup config token),
    runs them automatically with no prompts.
    """
    from hazel.cli.commands import _run_setup_skills
    from hazel.config.paths import get_pending_setup_skills_path

    pending_path = get_pending_setup_skills_path()

    if auto_instructions:
        console.print()
        console.print(
            Panel(
                f"[bold]Step 3 of {total_steps} — Setup Skills[/bold]\n\n"
                "Installing skills from your config...",
                border_style="blue",
            )
        )
        console.print()
        _run_setup_skills(config, auto_instructions)
        _clear_pending_instructions(pending_path)
        console.print("[green]✓[/green] Skills setup complete")
        return

    # No config token — skip entirely during quickstart
    # (user can run `hazel setup-skills` later)
    return


def run_quickstart(config: Config, has_setup_config: bool = False) -> tuple[Config, bool]:
    """Run the quickstart wizard.

    Returns (config, should_save) tuple.
    *has_setup_config* changes step count and intro text.
    """
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Quickstart requires an interactive terminal.\n"
            "If you're running via a piped install script, try:\n"
            "  hazel quickstart"
        )

    from hazel import __logo__, __version__

    total_steps = 5 if has_setup_config else 2

    steps_list = (
        "  1. Set up your LLM provider (AI brain)\n"
        "  2. Connect a chat channel (Telegram)"
    )
    if has_setup_config:
        steps_list += (
            "\n  3. Install skills from your config"
            "\n  4. Configure actions & workflows"
            "\n  5. Chat with your new assistant"
        )

    console.print()
    console.print(
        Panel(
            f"{__logo__} [bold cyan]Hazel Quickstart[/bold cyan]  [dim]v{__version__}[/dim]\n\n"
            f"Let's get you up and running:\n"
            f"{steps_list}",
            border_style="green",
        )
    )

    step = 1
    while step <= 2:
        if step == 1:
            if not _step_provider(config, total_steps=total_steps):
                console.print("[yellow]Setup cancelled.[/yellow]")
                return config, False
            step = 2
        elif step == 2:
            result = _step_channel(config, total_steps=total_steps)
            if result == _GO_BACK:
                step = 1
                continue
            if not result:
                console.print("[yellow]Setup cancelled.[/yellow]")
                return config, False
            step = 3  # done

    return config, True


def _step_setup_user_actions(config: Config, auto_instructions: str | None = None,
                             total_steps: int = 5) -> None:
    """Run user actions setup through the agent.

    If *auto_instructions* is provided (from a setup config token),
    runs them automatically with no prompts.
    """
    from hazel.cli.commands import _run_setup_user_actions
    from hazel.config.paths import get_pending_setup_user_actions_path

    pending_path = get_pending_setup_user_actions_path()

    if auto_instructions:
        console.print()
        console.print(
            Panel(
                f"[bold]Step 4 of {total_steps} — Setup User Actions[/bold]\n\n"
                "Configuring actions and workflows from your config...",
                border_style="blue",
            )
        )
        console.print()
        _run_setup_user_actions(config, auto_instructions)
        _clear_pending_instructions(pending_path)
        console.print("[green]✓[/green] User actions setup complete")
        return

    # No config token — skip entirely during quickstart
    # (user can run `hazel setup-user-actions` later)
    return


def run_quickstart_post_save(
    config: Config, setup_config_data: dict[str, str] | None = None
) -> None:
    """Run post-save setup steps (skills + user actions).

    Called by the quickstart command after saving config, so the agent
    has a working provider available.

    If *setup_config_data* is provided (from a --setup-config token),
    the skillsSetup and userActions values are fed directly and run
    automatically with no prompts.  Without a config token, these
    steps are skipped (user can run them later via standalone commands).
    """
    skills_instructions = (setup_config_data or {}).get("skillsSetup") or None
    actions_instructions = (setup_config_data or {}).get("userActions") or None

    total_steps = 5 if (skills_instructions or actions_instructions) else 2

    _step_setup_skills(config, auto_instructions=skills_instructions,
                       total_steps=total_steps)
    _step_setup_user_actions(config, auto_instructions=actions_instructions,
                             total_steps=total_steps)
