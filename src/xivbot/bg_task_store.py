"""
Persistent storage for background tasks in XivBot.

Disk layout:
    <workspace>/bg_tasks/<chat_id>/<task_id>.json      ← task metadata
    <workspace>/bg_tasks/<chat_id>/<task_id>_result.md ← result (when done)

Each metadata JSON holds:
    {
      "task_id":      "20260225_143000_a1b2c3",
      "chat_id":      "8376142125",
      "prompt":       "帮我调研 50 篇今年的 agentic memory paper",
      "status":       "pending" | "running" | "done" | "failed" | "cancelled",
      "created_at":   "2026-02-25T14:30:00",
      "started_at":   "2026-02-25T14:30:01",
      "finished_at":  "2026-02-25T15:10:00",
      "result_file":  "/path/to/<task_id>_result.md",
      "error":        null
    }
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import config as cfg


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class BackgroundTask:
    task_id: str
    chat_id: str
    prompt: str
    status: str                    # pending / running / done / failed / cancelled
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result_file: Optional[str] = None
    error: Optional[str] = None

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "task_id":     self.task_id,
            "chat_id":     self.chat_id,
            "prompt":      self.prompt,
            "status":      self.status,
            "created_at":  self.created_at,
            "started_at":  self.started_at,
            "finished_at": self.finished_at,
            "result_file": self.result_file,
            "error":       self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BackgroundTask":
        return cls(
            task_id=d["task_id"],
            chat_id=d["chat_id"],
            prompt=d["prompt"],
            status=d.get("status", "pending"),
            created_at=d["created_at"],
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            result_file=d.get("result_file"),
            error=d.get("error"),
        )

    def short_id(self) -> str:
        """Last 6 chars of task_id for display."""
        return self.task_id[-6:]

    def prompt_preview(self, max_len: int = 50) -> str:
        return self.prompt[:max_len] + ("…" if len(self.prompt) > max_len else "")


# ── Store ─────────────────────────────────────────────────────────────────────

class BackgroundTaskStore:
    """
    Thread-safe read/write access to per-chat background task records on disk.
    """

    def __init__(self):
        self._lock = threading.Lock()

    # ── Directory helpers ─────────────────────────────────────────────────────

    def _chat_dir(self, chat_id: str) -> Path:
        d = cfg.get_workspace_dir() / "bg_tasks" / chat_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _task_path(self, chat_id: str, task_id: str) -> Path:
        return self._chat_dir(chat_id) / f"{task_id}.json"

    def result_path(self, chat_id: str, task_id: str) -> Path:
        return self._chat_dir(chat_id) / f"{task_id}_result.md"

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(self, chat_id: str, prompt: str) -> BackgroundTask:
        """Create and persist a new pending task."""
        now = _now()
        task_id = _make_id()
        task = BackgroundTask(
            task_id=task_id,
            chat_id=chat_id,
            prompt=prompt,
            status="pending",
            created_at=now,
        )
        self.save(task)
        return task

    def save(self, task: BackgroundTask) -> None:
        """Persist task metadata to disk."""
        with self._lock:
            path = self._task_path(task.chat_id, task.task_id)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)

    def load(self, chat_id: str, task_id: str) -> Optional[BackgroundTask]:
        """Load a task from disk. Returns None if not found."""
        path = self._task_path(chat_id, task_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return BackgroundTask.from_dict(json.load(f))
        except (json.JSONDecodeError, KeyError):
            return None

    def list_tasks(self, chat_id: str) -> List[BackgroundTask]:
        """Return all tasks for a chat, sorted newest-first."""
        chat_dir = self._chat_dir(chat_id)
        tasks = []
        for p in chat_dir.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    tasks.append(BackgroundTask.from_dict(json.load(f)))
            except Exception:
                continue
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def update_status(
        self,
        chat_id: str,
        task_id: str,
        status: str,
        *,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        result_file: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Atomically update task status fields and persist."""
        task = self.load(chat_id, task_id)
        if task is None:
            return
        task.status = status
        if started_at is not None:
            task.started_at = started_at
        if finished_at is not None:
            task.finished_at = finished_at
        if result_file is not None:
            task.result_file = result_file
        if error is not None:
            task.error = error
        self.save(task)

    def resolve(self, chat_id: str, id_or_number: str) -> Optional[BackgroundTask]:
        """
        Resolve a task by index number (1-based from /bgtasks list) or short_id.
        Returns None if not found.
        """
        tasks = self.list_tasks(chat_id)
        token = id_or_number.strip()

        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(tasks):
                return tasks[idx]
            return None

        # Match by short_id or full task_id suffix
        for t in tasks:
            if t.task_id == token or t.task_id.endswith(token):
                return t
        return None


# ── Singleton accessor ────────────────────────────────────────────────────────

_store: Optional[BackgroundTaskStore] = None


def get_bg_task_store() -> BackgroundTaskStore:
    global _store
    if _store is None:
        _store = BackgroundTaskStore()
    return _store


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _make_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{ts}_{suffix}"
