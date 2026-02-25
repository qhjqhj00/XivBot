"""
Interactive setup wizard for XivBot.
Guides users through LLM provider config, DeepXiv API key, and bot platforms.
"""
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich import box

from . import config as cfg

console = Console()


def _section(title: str) -> None:
    console.print()
    console.rule(f"[bold cyan]{title}[/bold cyan]")
    console.print()


def _success(msg: str) -> None:
    console.print(f"[bold green]✓[/bold green] {msg}")


def _warn(msg: str) -> None:
    console.print(f"[bold yellow]⚠[/bold yellow]  {msg}")


def _error(msg: str) -> None:
    console.print(f"[bold red]✗[/bold red] {msg}")


def _info(msg: str) -> None:
    console.print(f"[dim]→[/dim] {msg}")


# ── deepxiv SDK check ──────────────────────────────────────────────────────────

def check_and_install_deepxiv() -> bool:
    """Return True when deepxiv-sdk is available (installing if needed)."""
    try:
        import deepxiv_sdk  # noqa: F401
        _success("deepxiv-sdk is already installed.")
        return True
    except ImportError:
        pass

    _warn("deepxiv-sdk is not installed.")
    if not Confirm.ask("Install deepxiv-sdk[agent] now?", default=True):
        _error("deepxiv-sdk is required. Aborting.")
        return False

    console.print("[dim]Running: pip install 'deepxiv-sdk[agent]'…[/dim]")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "deepxiv-sdk[agent]"],
        capture_output=False,
    )
    if result.returncode != 0:
        _error("Installation failed. Please run: pip install 'deepxiv-sdk[agent]'")
        return False

    _success("deepxiv-sdk installed successfully.")
    return True


# ── LLM provider wizard ────────────────────────────────────────────────────────

def _show_provider_table() -> None:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("Key", style="bold")
    table.add_column("Provider")
    table.add_column("Suggested models", style="dim")

    for idx, (key, info) in enumerate(cfg.PROVIDERS.items(), 1):
        models_preview = ", ".join(info["models"][:2])
        table.add_row(str(idx), key, info["name"], models_preview)

    console.print(table)


def configure_llm() -> bool:
    """Interactively configure the default LLM provider."""
    _section("Step 1 · LLM Provider")
    console.print("Choose the AI model provider to power XivBot's agent.\n")

    _show_provider_table()

    provider_keys = list(cfg.PROVIDERS.keys())
    while True:
        raw = Prompt.ask(
            "Enter provider number or key (e.g. [bold]1[/bold] or [bold]openai[/bold])"
        ).strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(provider_keys):
                provider_key = provider_keys[idx]
                break
        elif raw in cfg.PROVIDERS:
            provider_key = raw
            break
        _warn("Invalid choice, please try again.")

    provider_info = cfg.PROVIDERS[provider_key]
    console.print(f"\nSelected: [bold cyan]{provider_info['name']}[/bold cyan]")
    _info(f"Get your API key at: {provider_info['docs_url']}")
    console.print()

    api_key = Prompt.ask(
        f"Enter your {provider_info['name']} API key",
        password=True,
    ).strip()
    if not api_key:
        _error("API key cannot be empty.")
        return False

    # Model selection
    models = provider_info["models"]
    console.print("\nAvailable models:")
    for i, m in enumerate(models, 1):
        console.print(f"  [dim]{i}.[/dim] {m}")
    console.print(f"  [dim]{len(models)+1}.[/dim] Enter custom model name")

    default_model = models[0]
    while True:
        model_raw = Prompt.ask(
            f"Choose model (1-{len(models)+1})",
            default="1",
        ).strip()
        if model_raw.isdigit():
            m_idx = int(model_raw) - 1
            if 0 <= m_idx < len(models):
                model = models[m_idx]
                break
            elif m_idx == len(models):
                model = Prompt.ask("Enter custom model name").strip()
                if model:
                    break
        else:
            # Treat as direct model name
            model = model_raw
            break
        _warn("Invalid choice, please try again.")

    base_url = provider_info["base_url"]

    # Save to config
    c = cfg.load_config()
    c["llm"] = {
        "provider": provider_key,
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
    }
    cfg.save_config(c)

    _success(
        f"LLM configured: [bold]{provider_info['name']}[/bold] / [bold]{model}[/bold]"
    )
    return True


# ── DeepXiv API key ────────────────────────────────────────────────────────────

def configure_deepxiv() -> bool:
    """Interactively configure the DeepXiv API token."""
    _section("Step 2 · DeepXiv API Key")
    console.print("XivBot uses the DeepXiv API to access arXiv papers.\n")
    _info("Get your free token at: [link]https://data.rag.ac.cn/register[/link]")
    console.print()

    current = cfg.get_deepxiv_token()
    if current:
        masked = current[:8] + "…" + current[-4:] if len(current) > 12 else "***"
        console.print(f"Current token: [dim]{masked}[/dim]")
        if not Confirm.ask("Update DeepXiv token?", default=False):
            _success("Keeping existing DeepXiv token.")
            return True

    token = Prompt.ask("Enter your DeepXiv API token", password=True).strip()
    if not token:
        _error("Token cannot be empty.")
        return False

    # Quick validity check
    console.print("[dim]Verifying token…[/dim]")
    try:
        from deepxiv_sdk import Reader
        reader = Reader(token=token)
        result = reader.search("test", size=1)
        if result is None:
            _warn("Could not verify token (API unreachable). Saving anyway.")
        else:
            _success("Token verified successfully!")
    except Exception as exc:
        _warn(f"Verification skipped: {exc}. Saving token anyway.")

    cfg.set_value("deepxiv.api_key", token)
    _success("DeepXiv token saved.")
    return True


# ── Bot platform wizard ────────────────────────────────────────────────────────

def configure_bots() -> None:
    """Interactively configure Feishu and/or Telegram bots."""
    _section("Step 3 · Bot Platforms (optional)")
    console.print(
        "XivBot can forward queries from [bold]Feishu[/bold] and [bold]Telegram[/bold].\n"
        "Press Enter to skip a platform.\n"
    )

    if Confirm.ask("Configure [bold]Feishu[/bold] bot?", default=False):
        _configure_feishu()
    else:
        _info("Feishu skipped.")

    console.print()

    if Confirm.ask("Configure [bold]Telegram[/bold] bot?", default=False):
        _configure_telegram()
    else:
        _info("Telegram skipped.")


def _configure_feishu() -> None:
    console.print()
    console.print(Panel(
        "[bold]Feishu App setup[/bold]\n\n"
        "1. Go to [link]https://open.feishu.cn/[/link] → Create custom app\n"
        "2. Enable [bold]Bot[/bold] capability under 'Add capabilities'\n"
        "3. Subscribe to [bold]im.message.receive_v1[/bold] event\n"
        "4. Set Event callback URL to [bold]http://<your-server>:<port>/feishu/event[/bold]\n"
        "5. Copy App ID, App Secret, Verification Token from the console",
        title="Feishu Setup Guide",
        border_style="blue",
    ))

    app_id = Prompt.ask("App ID (cli_...)").strip()
    app_secret = Prompt.ask("App Secret", password=True).strip()
    verification_token = Prompt.ask("Verification Token").strip()
    encrypt_key = Prompt.ask(
        "Encrypt Key [dim](leave empty if not using encryption)[/dim]",
        default="",
    ).strip() or None
    port = Prompt.ask("Local webhook port", default="8080").strip()

    if not all([app_id, app_secret, verification_token]):
        _warn("Skipping Feishu config – required fields were empty.")
        return

    c = cfg.load_config()
    c["bots"]["feishu"] = {
        "enabled": True,
        "app_id": app_id,
        "app_secret": app_secret,
        "verification_token": verification_token,
        "encrypt_key": encrypt_key,
        "port": int(port),
    }
    cfg.save_config(c)

    # Send test message
    _test_feishu(app_id, app_secret, port)


def _test_feishu(app_id: str, app_secret: str, port: str) -> None:
    """Send a test message to verify Feishu credentials."""
    console.print("\n[dim]Verifying Feishu credentials by fetching a tenant token…[/dim]")
    try:
        import requests
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            _success("Feishu credentials verified. Bot is ready!")
            console.print(
                "[dim]Tip: Start the service with [bold]xivbot start[/bold] "
                f"and point your Feishu event URL to port {port}.[/dim]"
            )
        else:
            _warn(f"Feishu returned an error: {data.get('msg')}. Please check your credentials.")
    except Exception as exc:
        _warn(f"Could not reach Feishu API: {exc}")


def _configure_telegram() -> None:
    console.print()
    console.print(Panel(
        "[bold]Telegram Bot setup[/bold]\n\n"
        "1. Open Telegram and message [bold]@BotFather[/bold]\n"
        "2. Send [bold]/newbot[/bold] and follow the instructions\n"
        "3. Copy the bot token (looks like 123456789:AAH…)",
        title="Telegram Setup Guide",
        border_style="blue",
    ))

    bot_token = Prompt.ask("Bot token").strip()
    if not bot_token:
        _warn("Skipping Telegram config – token was empty.")
        return

    # Verify token
    console.print("[dim]Verifying Telegram bot token…[/dim]")
    try:
        import requests
        resp = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            bot_name = data["result"].get("username", "unknown")
            _success(f"Telegram bot verified: [bold]@{bot_name}[/bold]")

            # Send a test message
            owner_id = Prompt.ask(
                "Enter your Telegram user ID to receive a test message "
                "[dim](find it via @userinfobot)[/dim]",
                default="",
            ).strip()
            if owner_id:
                send_resp = requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": owner_id,
                        "text": "✅ XivBot is connected! Send me a research question and I'll get to work.",
                    },
                    timeout=10,
                )
                if send_resp.json().get("ok"):
                    _success("Test message sent! Check your Telegram.")
                else:
                    _warn(f"Could not send test message: {send_resp.json().get('description')}")
        else:
            _warn(f"Telegram returned an error: {data.get('description')}")
            return
    except Exception as exc:
        _warn(f"Could not reach Telegram API: {exc}. Saving token anyway.")

    c = cfg.load_config()
    c["bots"]["telegram"] = {
        "enabled": True,
        "bot_token": bot_token,
    }
    cfg.save_config(c)
    _success("Telegram bot configured.")


# ── Full wizard ────────────────────────────────────────────────────────────────

def configure_workspace() -> None:
    """Interactively configure the local workspace directory."""
    _section("Step 0 · Workspace Directory")
    console.print(
        "XivBot stores conversation sessions, context and notes in a local workspace.\n"
    )

    current = cfg.get("workspace.path") or str(Path.home() / "xivbot_workspace")
    console.print(f"Current path: [dim]{current}[/dim]")

    raw = Prompt.ask("Workspace directory", default=current).strip()
    path = Path(raw).expanduser().resolve()

    try:
        for sub in ("sessions", "context", "notes"):
            (path / sub).mkdir(parents=True, exist_ok=True)
        cfg.set_value("workspace.path", str(path))
        _success(f"Workspace ready at [bold]{path}[/bold]")
    except OSError as exc:
        _warn(f"Could not create workspace: {exc}. Using default.")


def run_full_wizard() -> None:
    """Run the complete interactive setup wizard."""
    console.print(
        Panel(
            "[bold cyan]Welcome to XivBot Setup[/bold cyan]\n\n"
            "This wizard will configure:\n"
            "  0. Workspace directory (sessions, context, notes)\n"
            "  1. LLM provider (the AI brain)\n"
            "  2. DeepXiv API key (paper access)\n"
            "  3. Bot platforms (Feishu / Telegram)",
            title="XivBot v0.1",
            border_style="cyan",
            padding=(1, 4),
        )
    )

    # Check/install deepxiv-sdk first
    if not check_and_install_deepxiv():
        sys.exit(1)

    # Workspace
    configure_workspace()

    # LLM provider
    if not configure_llm():
        sys.exit(1)

    # DeepXiv token
    if not configure_deepxiv():
        sys.exit(1)

    # Bot platforms
    configure_bots()

    # Done
    _section("Setup Complete")
    console.print(Panel(
        "[bold green]XivBot is configured![/bold green]\n\n"
        "Start the bot service:\n"
        "  [bold]xivbot start[/bold]\n\n"
        "Re-run this wizard anytime:\n"
        "  [bold]xivbot config[/bold]",
        border_style="green",
        padding=(1, 4),
    ))
