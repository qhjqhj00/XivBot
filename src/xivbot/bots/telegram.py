"""
Telegram bot integration for XivBot — raw HTTP API, no python-telegram-bot.

Uses long-polling (getUpdates with timeout=30) in a plain thread.
Only requires `requests`, which is already a core dependency.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

import requests
from rich.console import Console

from .base import BotBase
from .commands import CommandsMixin

console = Console()

BASE_URL = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30
REQUEST_TIMEOUT = 40
RETRY_SLEEP = 5


class TelegramBot(CommandsMixin, BotBase):
    """Telegram bot adapter using the raw HTTP Bot API."""

    def __init__(self, bot_token: str, verbose: bool = False):
        super().__init__("Telegram", verbose)
        self._init_commands()
        self.bot_token = bot_token
        self._running = False
        self._offset = 0

    # ── BotBase interface ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        console.log("[Telegram] Bot started. Polling for messages…")
        while self._running:
            try:
                self._poll_once()
            except requests.exceptions.Timeout:
                pass
            except requests.exceptions.ConnectionError as exc:
                console.log(f"[Telegram] Connection error: {exc}. Retrying in {RETRY_SLEEP}s…")
                time.sleep(RETRY_SLEEP)
            except Exception as exc:
                console.log(f"[Telegram] Unexpected error: {exc}. Retrying in {RETRY_SLEEP}s…")
                time.sleep(RETRY_SLEEP)

    def stop(self) -> None:
        self._running = False
        console.log("[Telegram] Bot stopped.")

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll_once(self) -> None:
        resp = requests.get(
            _url(self.bot_token, "getUpdates"),
            params={
                "offset": self._offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ["message"],
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        if not data.get("ok"):
            console.log(f"[Telegram] getUpdates error: {data.get('description')}")
            time.sleep(RETRY_SLEEP)
            return
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            self._dispatch(update)

    def _dispatch(self, update: dict) -> None:
        message = update.get("message", {})
        if not message:
            return
        text = (message.get("text") or "").strip()
        chat_id = str(message["chat"]["id"])
        username = (
            message.get("from", {}).get("username")
            or message.get("from", {}).get("first_name")
            or chat_id
        )
        if not text:
            return
        if self.verbose:
            console.log(f"[Telegram] @{username}: {text!r}")

        # CommandsMixin handles all /commands and pending-state flows
        if self._dispatch_command(chat_id, text):
            return

        # Regular message → agent
        def reply(answer: str) -> None:
            self._send(chat_id, answer)

        threading.Thread(
            target=self.on_message,
            args=(text, reply, chat_id),
            daemon=True,
        ).start()

    # ── Platform send implementation ──────────────────────────────────────────

    def _send(self, chat_id: str, text: str) -> None:
        plain = _md_to_plain(text)
        for chunk in _split(plain, 4096):
            self._post_message(chat_id, chunk)

    def _send_document(self, chat_id: str, filepath: str, filename: str) -> bool:
        try:
            with open(filepath, "rb") as f:
                resp = requests.post(
                    _url(self.bot_token, "sendDocument"),
                    data={"chat_id": chat_id},
                    files={"document": (filename, f, "text/markdown")},
                    timeout=30,
                )
            result = resp.json()
            if result.get("ok"):
                return True
            if self.verbose:
                console.log(f"[Telegram] sendDocument error: {result.get('description')}")
            return False
        except Exception as exc:
            if self.verbose:
                console.log(f"[Telegram] sendDocument exception: {exc}")
            return False

    def _post_message(self, chat_id: str, text: str) -> bool:
        try:
            resp = requests.post(
                _url(self.bot_token, "sendMessage"),
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            result = resp.json()
            if result.get("ok"):
                return True
            if self.verbose:
                console.log(f"[Telegram] sendMessage error: {result.get('description')}")
            return False
        except Exception as exc:
            if self.verbose:
                console.log(f"[Telegram] sendMessage exception: {exc}")
            return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _url(token: str, method: str) -> str:
    return BASE_URL.format(token=token, method=method)


def _md_to_plain(text: str) -> str:
    text = re.sub(r"```[^\n]*\n(.*?)```", lambda m: m.group(1).strip(), text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)

    def _heading(m):
        level = len(m.group(1))
        title = m.group(2).strip()
        if level == 1:
            return f"\n{'━' * min(len(title) + 4, 40)}\n  {title.upper()}\n{'━' * min(len(title) + 4, 40)}"
        elif level == 2:
            return f"\n▌ {title.upper()}"
        else:
            return f"\n• {title}"
    text = re.sub(r"^(#{1,6})\s+(.+)$", _heading, text, flags=re.MULTILINE)

    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*([^*\n]+?)\*", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"^[-*_]{3,}\s*$", "─" * 32, text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*>\s?", "│ ", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*[-*+]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks
