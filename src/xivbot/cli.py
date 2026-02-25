"""
XivBot CLI entry point.

Commands:
  xivbot config   – interactive setup wizard
  xivbot start    – start the bot service (Feishu / Telegram)
  xivbot chat     – interactive terminal chat with the agent
  xivbot ask      – single-shot query from the command line
  xivbot status   – show current configuration
"""
from __future__ import annotations

import sys
import threading
import time

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from . import config as cfg

console = Console()


# ── CLI group ─────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(package_name="xivbot", prog_name="xivbot")
def main():
    """XivBot – terminal agent for arXiv paper research, powered by DeepXiv SDK."""


# ── xivbot config ─────────────────────────────────────────────────────────────

@main.command()
@click.option("--llm-only", is_flag=True, help="Reconfigure LLM provider only")
@click.option("--deepxiv-only", is_flag=True, help="Reconfigure DeepXiv token only")
@click.option("--bots-only", is_flag=True, help="Reconfigure bot platforms only")
@click.option("--workspace-only", is_flag=True, help="Reconfigure workspace directory only")
def config(llm_only, deepxiv_only, bots_only, workspace_only):
    """Interactive setup wizard: LLM provider, DeepXiv key, bot platforms."""
    from .wizard import (
        check_and_install_deepxiv,
        configure_bots,
        configure_deepxiv,
        configure_llm,
        configure_workspace,
        run_full_wizard,
    )

    if llm_only:
        configure_llm()
    elif deepxiv_only:
        if not check_and_install_deepxiv():
            sys.exit(1)
        configure_deepxiv()
    elif bots_only:
        configure_bots()
    elif workspace_only:
        configure_workspace()
    else:
        run_full_wizard()


# ── xivbot status ─────────────────────────────────────────────────────────────

@main.command()
def status():
    """Show current XivBot configuration."""
    import os
    from pathlib import Path
    from .session_store import get_store

    c = cfg.load_config()
    llm = c.get("llm", {})
    bots = c.get("bots", {})
    workspace_path = Path(c.get("workspace", {}).get("path") or Path.home() / "xivbot_workspace")

    table = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 2))
    table.add_column("Key", style="bold cyan", width=24)
    table.add_column("Value")

    # LLM
    provider_key = llm.get("provider") or "—"
    provider_name = cfg.PROVIDERS.get(provider_key, {}).get("name", provider_key)
    table.add_row("LLM Provider", f"{provider_name} ({provider_key})")
    table.add_row("LLM Model", llm.get("model") or "—")
    table.add_row("LLM API Key", _mask(llm.get("api_key")))
    table.add_row("LLM Base URL", llm.get("base_url") or "—")

    table.add_section()
    table.add_row("DeepXiv Token", _mask(cfg.get_deepxiv_token()))

    table.add_section()
    feishu = bots.get("feishu", {})
    table.add_row(
        "Feishu Bot",
        "[green]enabled[/green]" if feishu.get("enabled") else "[dim]disabled[/dim]",
    )
    if feishu.get("enabled"):
        table.add_row("  App ID", feishu.get("app_id") or "—")
        table.add_row("  Port", str(feishu.get("port", 8080)))

    tg = bots.get("telegram", {})
    table.add_row(
        "Telegram Bot",
        "[green]enabled[/green]" if tg.get("enabled") else "[dim]disabled[/dim]",
    )
    if tg.get("enabled"):
        table.add_row("  Token", _mask(tg.get("bot_token")))

    table.add_section()
    # Count total sessions across all chats
    total_sessions = 0
    if workspace_path.exists():
        sessions_root = workspace_path / "sessions"
        if sessions_root.exists():
            for chat_dir in sessions_root.iterdir():
                if chat_dir.is_dir():
                    total_sessions += sum(
                        1 for f in chat_dir.glob("*.json")
                        if f.name != "_state.json"
                    )
    from .memory_store import get_memory_store
    mem_stats = get_memory_store().stats()

    table.add_row("Workspace", str(workspace_path))
    table.add_row("  Sessions stored", str(total_sessions))
    table.add_row("  Papers memorised", str(mem_stats["papers_memorised"]))
    table.add_row("  Days with activity", str(mem_stats["days_with_activity"]))

    table.add_section()
    table.add_row("Config file", str(cfg.CONFIG_FILE))

    console.print(
        Panel(table, title="[bold]XivBot Status[/bold]", border_style="cyan")
    )


# ── xivbot chat ───────────────────────────────────────────────────────────────

@main.command()
@click.option("--verbose", "-v", is_flag=True, help="Show agent reasoning steps")
def chat(verbose):
    """Start an interactive terminal chat with the XivBot agent."""
    _check_config_or_exit()
    from .agent_runner import chat_loop
    chat_loop(verbose=verbose)


# ── xivbot ask ────────────────────────────────────────────────────────────────

@main.command()
@click.argument("question", nargs=-1, required=True)
@click.option("--verbose", "-v", is_flag=True, help="Show agent reasoning steps")
def ask(question, verbose):
    """Ask a single research question and print the answer.

    Example:
        xivbot ask "What are the best papers on RAG in 2024?"
    """
    _check_config_or_exit()
    from .agent_runner import run_query

    full_question = " ".join(question)
    console.print(f"\n[bold cyan]Q:[/bold cyan] {full_question}\n")

    with console.status("[bold cyan]Thinking…[/bold cyan]", spinner="dots"):
        answer = run_query(full_question, verbose=verbose)

    from rich.markdown import Markdown
    console.print(Panel(Markdown(answer), title="[bold]Answer[/bold]", border_style="blue"))


# ── xivbot start ─────────────────────────────────────────────────────────────

@main.command()
@click.option("--verbose", "-v", is_flag=True, help="Show agent reasoning steps in terminal")
@click.option("--feishu", "force_feishu", is_flag=True, help="Force start Feishu bot")
@click.option("--telegram", "force_telegram", is_flag=True, help="Force start Telegram bot")
def start(verbose, force_feishu, force_telegram):
    """Start the XivBot service (Feishu / Telegram bots).

    \b
    The terminal displays live execution logs.
    Press Ctrl-C to stop.
    """
    _check_config_or_exit()

    c = cfg.load_config()
    bots_cfg = c.get("bots", {})
    feishu_cfg = bots_cfg.get("feishu", {})
    tg_cfg = bots_cfg.get("telegram", {})

    want_feishu = force_feishu or feishu_cfg.get("enabled", False)
    want_telegram = force_telegram or tg_cfg.get("enabled", False)

    if not want_feishu and not want_telegram:
        console.print(
            "[bold yellow]No bot platforms are enabled.[/bold yellow]\n"
            "Configure them first with [bold]xivbot config --bots-only[/bold], "
            "or use the interactive chat: [bold]xivbot chat[/bold]"
        )
        sys.exit(0)

    active_bots = []

    # Message handler shared across all bots
    def handle_message(text: str, reply_fn, session_id: str = "default") -> None:
        _process_and_reply(text, reply_fn, session_id=session_id, verbose=verbose)

    # ── Feishu ────────────────────────────────────────────────────────────────
    if want_feishu:
        if not feishu_cfg.get("app_id"):
            console.print("[red]Feishu is not fully configured. Run xivbot config --bots-only.[/red]")
        else:
            from .bots.feishu import FeishuBot
            feishu_bot = FeishuBot(
                app_id=feishu_cfg["app_id"],
                app_secret=feishu_cfg["app_secret"],
                verification_token=feishu_cfg["verification_token"],
                encrypt_key=feishu_cfg.get("encrypt_key"),
                port=feishu_cfg.get("port", 8080),
                verbose=verbose,
            )
            feishu_bot.set_message_handler(handle_message)
            active_bots.append(feishu_bot)

    # ── Telegram ──────────────────────────────────────────────────────────────
    if want_telegram:
        if not tg_cfg.get("bot_token"):
            console.print("[red]Telegram is not fully configured. Run xivbot config --bots-only.[/red]")
        else:
            from .bots.telegram import TelegramBot
            tg_bot = TelegramBot(
                bot_token=tg_cfg["bot_token"],
                verbose=verbose,
            )
            tg_bot.set_message_handler(handle_message)
            active_bots.append(tg_bot)

    if not active_bots:
        console.print("[red]No bots could be started.[/red]")
        sys.exit(1)

    # ── Display startup banner ─────────────────────────────────────────────────
    llm = c.get("llm", {})
    provider_name = cfg.PROVIDERS.get(llm.get("provider", ""), {}).get("name", "Unknown")

    console.print(
        Panel(
            f"[bold green]XivBot is running![/bold green]\n\n"
            + "\n".join(
                f"  [cyan]•[/cyan] {b.name} bot active"
                for b in active_bots
            )
            + f"\n\n[dim]LLM:[/dim] {provider_name} / {llm.get('model', '?')}\n"
            "[dim]Press Ctrl-C to stop.[/dim]",
            title="XivBot Service",
            border_style="green",
            padding=(1, 4),
        )
    )

    # Start bots (all but the last in daemon threads)
    threads = []
    for bot in active_bots[:-1]:
        t = bot.start_in_thread()
        threads.append((bot, t))

    try:
        # Run the last bot in the main thread (blocks until Ctrl-C)
        active_bots[-1].start()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Shutting down…[/bold yellow]")
    finally:
        for bot, _ in threads:
            bot.stop()
        active_bots[-1].stop()
        console.print("[dim]XivBot stopped.[/dim]")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_config_or_exit() -> None:
    if not cfg.is_configured():
        console.print(
            "[bold red]XivBot is not configured.[/bold red] "
            "Run [bold]xivbot config[/bold] to set it up."
        )
        sys.exit(1)


def _mask(value) -> str:
    if not value:
        return "[dim]—[/dim]"
    s = str(value)
    if len(s) <= 8:
        return "***"
    return s[:6] + "…" + s[-4:]


def _process_and_reply(text: str, reply_fn, session_id: str = "default", verbose: bool = False) -> None:
    """Run the agent and send the answer via reply_fn. Logs to terminal."""
    from .agent_runner import run_query, get_session_manager

    # /reset clears the active session conversation
    if text.strip() == "/reset":
        get_session_manager(verbose=verbose).reset_current(session_id)
        return

    console.log(f"[bold cyan][{session_id}] Query:[/bold cyan] {text!r}")
    start = time.time()

    answer = run_query(text, verbose=verbose, session_id=session_id)

    elapsed = time.time() - start
    console.log(
        f"[bold green][{session_id}] Answered[/bold green] in {elapsed:.1f}s "
        f"([dim]{len(answer)} chars[/dim])"
    )

    try:
        reply_fn(answer)
    except Exception as exc:
        console.log(f"[red]Failed to send reply: {exc}[/red]")


if __name__ == "__main__":
    main()
