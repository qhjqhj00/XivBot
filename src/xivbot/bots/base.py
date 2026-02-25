"""
Abstract base class for XivBot platform integrations.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Callable, Optional

from rich.console import Console

console = Console()


class BotBase(ABC):
    """
    Base class for all bot platform adapters.

    Subclasses implement `start()` and `stop()` and call
    `self.on_message(text, reply_fn)` when a user message arrives.
    """

    def __init__(self, name: str, verbose: bool = False):
        self.name = name
        self.verbose = verbose
        self._message_handler: Optional[Callable[[str, Callable[[str], None]], None]] = None
        self._thread: Optional[threading.Thread] = None

    def set_message_handler(
        self, handler: Callable[[str, Callable[[str], None], str], None]
    ) -> None:
        """
        Register the function that processes incoming messages.

        Args:
            handler: Callable(text, reply_fn, session_id) where reply_fn(answer)
                     sends the answer back to the user and session_id identifies
                     the conversation (e.g. Telegram chat_id or Feishu chat_id).
        """
        self._message_handler = handler

    def on_message(self, text: str, reply_fn: Callable[[str], None], session_id: str = "default") -> None:
        """Called by platform implementations when a message arrives."""
        if self._message_handler:
            if self.verbose:
                console.log(f"[{self.name}] [{session_id}] Received: {text!r}")
            self._message_handler(text, reply_fn, session_id)

    @abstractmethod
    def start(self) -> None:
        """Start listening for incoming messages (blocking)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the bot gracefully."""

    def start_in_thread(self) -> threading.Thread:
        """Launch start() in a daemon thread and return it."""
        self._thread = threading.Thread(target=self.start, daemon=True, name=self.name)
        self._thread.start()
        return self._thread
