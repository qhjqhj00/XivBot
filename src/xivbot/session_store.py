"""
Persistent session storage for XivBot.

Layout on disk:
    <workspace>/sessions/<chat_id>/<session_id>.json

Each JSON file holds:
    {
      "session_id":  "20260225_100523_a1b2c3",
      "name":        "Agent Memory Papers Feb 2026",
      "chat_id":     "8376142125",
      "created_at":  "2026-02-25T10:05:23",
      "updated_at":  "2026-02-25T10:35:00",
      "messages":    [{"role": "user"|"assistant", "content": "..."}]
    }
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import config as cfg


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Session:
    session_id: str
    chat_id: str
    name: str
    created_at: str
    updated_at: str
    messages: List[Dict] = field(default_factory=list)
    active: bool = True

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active": self.active,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            session_id=d["session_id"],
            chat_id=d["chat_id"],
            name=d.get("name", "Untitled"),
            created_at=d["created_at"],
            updated_at=d.get("updated_at", d["created_at"]),
            active=d.get("active", True),
            messages=d.get("messages", []),
        )

    def touch(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = _now()

    def short_id(self) -> str:
        """Last 6 chars of session_id for display."""
        return self.session_id[-6:]

    def summary_line(self, idx: int) -> str:
        n_turns = len([m for m in self.messages if m["role"] == "user"])
        return (
            f"{idx}. [{self.short_id()}] {self.name}  "
            f"({n_turns} turns · {self.updated_at[:16].replace('T', ' ')})"
        )


# ── Store ─────────────────────────────────────────────────────────────────────

class SessionStore:
    """
    Thread-safe read/write access to per-chat sessions on disk.
    """

    def __init__(self):
        self._lock = threading.Lock()

    # ── Directory helpers ─────────────────────────────────────────────────────

    def _chat_dir(self, chat_id: str) -> Path:
        d = cfg.get_workspace_dir() / "sessions" / chat_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _session_path(self, chat_id: str, session_id: str) -> Path:
        return self._chat_dir(chat_id) / f"{session_id}.json"

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(self, chat_id: str, name: str = "New Session") -> Session:
        """Create and persist a new session."""
        now = _now()
        session_id = _make_id()
        session = Session(
            session_id=session_id,
            chat_id=chat_id,
            name=name,
            created_at=now,
            updated_at=now,
        )
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        """Persist session to disk."""
        with self._lock:
            path = self._session_path(session.chat_id, session.session_id)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)

    def load(self, chat_id: str, session_id: str) -> Optional[Session]:
        """Load a session from disk. Returns None if not found."""
        path = self._session_path(chat_id, session_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return Session.from_dict(json.load(f))
        except (json.JSONDecodeError, KeyError):
            return None

    def list_sessions(self, chat_id: str, include_inactive: bool = False) -> List[Session]:
        """Return sessions for a chat, sorted newest-first. Excludes soft-deleted by default."""
        chat_dir = self._chat_dir(chat_id)
        sessions = []
        for p in chat_dir.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    s = Session.from_dict(json.load(f))
                    if include_inactive or s.active:
                        sessions.append(s)
            except Exception:
                continue
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def delete(self, chat_id: str, session_id: str) -> bool:
        """Soft-delete: mark session as inactive. Returns True if it existed."""
        session = self.load(chat_id, session_id)
        if session is None:
            return False
        session.active = False
        session.touch()
        self.save(session)
        return True

    def rename(self, chat_id: str, session_id: str, name: str) -> None:
        """Rename a session."""
        session = self.load(chat_id, session_id)
        if session:
            session.name = name
            session.touch()
            self.save(session)

    def append_message(
        self, chat_id: str, session_id: str, role: str, content: str
    ) -> None:
        """Append a message to an existing session and persist."""
        session = self.load(chat_id, session_id)
        if session:
            session.messages.append({"role": role, "content": content})
            session.touch()
            self.save(session)


# ── Per-chat active-session tracking ─────────────────────────────────────────

class ChatState:
    """
    Tracks which session is currently active for each Telegram/Feishu chat.
    Persisted as a tiny JSON file per chat_id.
    """

    def __init__(self):
        self._lock = threading.Lock()

    def _state_path(self, chat_id: str) -> Path:
        d = cfg.get_workspace_dir() / "sessions" / chat_id
        d.mkdir(parents=True, exist_ok=True)
        return d / "_state.json"

    def get_active(self, chat_id: str) -> Optional[str]:
        path = self._state_path(chat_id)
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                return json.load(f).get("active_session_id")
        except Exception:
            return None

    def set_active(self, chat_id: str, session_id: str) -> None:
        path = self._state_path(chat_id)
        with self._lock:
            with open(path, "w") as f:
                json.dump({"active_session_id": session_id}, f)


# ── Singleton accessors ───────────────────────────────────────────────────────

_store: Optional[SessionStore] = None
_chat_state: Optional[ChatState] = None


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store


def get_chat_state() -> ChatState:
    global _chat_state
    if _chat_state is None:
        _chat_state = ChatState()
    return _chat_state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _make_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{ts}_{suffix}"
