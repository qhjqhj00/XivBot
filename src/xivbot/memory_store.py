"""
XivBot Paper Memory Store.

Automatically records papers that the user has read deeply (via head / section /
preview / full-text skills) and makes them searchable by keyword and date.

Disk layout:
    <workspace>/memory/
    ├── papers/
    │   └── {arxiv_id}.json   ← canonical memory card per paper
    └── index.json            ← inverted index: by_date, by_session, by_chat

A memory card is written / updated in a background thread so it never adds
latency to the main agent loop.
"""
from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config as cfg

# ── Skills that should trigger a memory write ─────────────────────────────────

DEEP_SKILLS = {
    "get_paper_metadata",
    "get_paper_brief",
    "get_paper_preview",
    "read_paper_section",
    "get_full_paper",
    "get_pmc_metadata",
    "get_pmc_full",
}


# ── Data model ────────────────────────────────────────────────────────────────

class PaperMemory:
    """In-memory representation of a paper memory card."""

    def __init__(
        self,
        arxiv_id: str,
        title: str = "",
        tldr: str = "",
        keywords: Optional[List[str]] = None,
        abstract: str = "",
        categories: Optional[List[str]] = None,
        access_log: Optional[List[Dict]] = None,
    ):
        self.arxiv_id = arxiv_id
        self.title = title
        self.tldr = tldr
        self.keywords: List[str] = keywords or []
        self.abstract = abstract
        self.categories: List[str] = categories or []
        self.access_log: List[Dict] = access_log or []

    @property
    def first_seen(self) -> Optional[str]:
        return self.access_log[0]["date"] if self.access_log else None

    @property
    def last_seen(self) -> Optional[str]:
        return self.access_log[-1]["date"] if self.access_log else None

    def add_access(self, date_str: str, session_id: str, chat_id: str, skill: str) -> None:
        self.access_log.append(
            {"date": date_str, "session_id": session_id, "chat_id": chat_id, "skill": skill}
        )

    def dates_accessed(self) -> List[str]:
        return list(dict.fromkeys(e["date"] for e in self.access_log))

    def sessions_accessed(self) -> List[str]:
        return list(dict.fromkeys(e["session_id"] for e in self.access_log))

    def to_dict(self) -> dict:
        return {
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "tldr": self.tldr,
            "keywords": self.keywords,
            "abstract": self.abstract,
            "categories": self.categories,
            "access_log": self.access_log,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PaperMemory":
        return cls(
            arxiv_id=d["arxiv_id"],
            title=d.get("title", ""),
            tldr=d.get("tldr", ""),
            keywords=d.get("keywords", []),
            abstract=d.get("abstract", ""),
            categories=d.get("categories", []),
            access_log=d.get("access_log", []),
        )

    def brief_text(self) -> str:
        """Short human-readable summary for recall results."""
        kw = ", ".join(self.keywords[:5]) if self.keywords else "—"
        dates = ", ".join(self.dates_accessed()[-3:])
        lines = [
            f"[{self.arxiv_id}] {self.title}",
            f"  https://arxiv.org/abs/{self.arxiv_id}",
            f"  Keywords: {kw}",
            f"  Last accessed: {dates}",
        ]
        if self.tldr:
            lines.append(f"  TLDR: {self.tldr[:200]}")
        return "\n".join(lines)


# ── Store ─────────────────────────────────────────────────────────────────────

class MemoryStore:
    """
    Thread-safe paper memory store backed by JSON files in the workspace.
    """

    def __init__(self):
        self._lock = threading.Lock()

    # ── Paths ─────────────────────────────────────────────────────────────────

    def _memory_dir(self) -> Path:
        d = cfg.get_workspace_dir() / "memory"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _papers_dir(self) -> Path:
        d = self._memory_dir() / "papers"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_path(self) -> Path:
        return self._memory_dir() / "index.json"

    # ── Index ─────────────────────────────────────────────────────────────────

    def _load_index(self) -> dict:
        p = self._index_path()
        if not p.exists():
            return {"by_date": {}, "by_session": {}, "by_chat": {}}
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"by_date": {}, "by_session": {}, "by_chat": {}}

    def _save_index(self, idx: dict) -> None:
        with open(self._index_path(), "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)

    def _update_index(self, arxiv_id: str, date_str: str, session_id: str, chat_id: str) -> None:
        idx = self._load_index()
        for bucket, key in [
            ("by_date", date_str),
            ("by_session", session_id),
            ("by_chat", chat_id),
        ]:
            bucket_data = idx.setdefault(bucket, {})
            ids = bucket_data.setdefault(key, [])
            if arxiv_id not in ids:
                ids.append(arxiv_id)
        self._save_index(idx)

    # ── Paper card ────────────────────────────────────────────────────────────

    def _card_path(self, arxiv_id: str) -> Path:
        safe_id = arxiv_id.replace("/", "_")
        return self._papers_dir() / f"{safe_id}.json"

    def load_card(self, arxiv_id: str) -> Optional[PaperMemory]:
        p = self._card_path(arxiv_id)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return PaperMemory.from_dict(json.load(f))
        except Exception:
            return None

    def save_card(self, memory: PaperMemory) -> None:
        p = self._card_path(memory.arxiv_id)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(memory.to_dict(), f, ensure_ascii=False, indent=2)

    # ── Public API ────────────────────────────────────────────────────────────

    def record_access(
        self,
        arxiv_id: str,
        session_id: str,
        chat_id: str,
        skill_name: str,
        reader,
    ) -> None:
        """
        Record that a paper was accessed via a deep skill.
        Fetches brief info from the API if not already stored.
        Safe to call from a background thread.
        """
        today = date.today().isoformat()
        with self._lock:
            card = self.load_card(arxiv_id) or PaperMemory(arxiv_id=arxiv_id)

            # Enrich with brief + metadata if we don't have it yet
            if not card.title:
                try:
                    brief = reader.brief(arxiv_id)
                    if brief:
                        card.title = brief.get("title", "")
                        card.tldr = brief.get("tldr", "")
                        kw = brief.get("keywords", [])
                        card.keywords = kw if isinstance(kw, list) else [kw]
                        card.abstract = brief.get("abstract", "")
                except Exception:
                    pass
            # Enrich with arXiv categories if not yet stored
            if not card.categories:
                try:
                    meta = reader.head(arxiv_id)
                    if meta:
                        cats = meta.get("categories") or meta.get("category") or []
                        if isinstance(cats, str):
                            cats = [c.strip() for c in cats.replace(",", " ").split()]
                        card.categories = [c for c in cats if c]
                except Exception:
                    pass

            card.add_access(today, session_id, chat_id, skill_name)
            self.save_card(card)
            self._update_index(arxiv_id, today, session_id, chat_id)

    def recall(
        self,
        query: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        session_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        limit: int = 15,
    ) -> List[PaperMemory]:
        """
        Search memory by keyword + optional date / session filter.

        Scoring: count of query tokens that appear in
        title + tldr + keywords + abstract (case-insensitive).
        """
        # Determine candidate arxiv_ids from index
        idx = self._load_index()
        candidates: Optional[set] = None

        if date_from or date_to:
            date_ids: set = set()
            lo = date_from or "0000-00-00"
            hi = date_to or "9999-99-99"
            for d, ids in idx.get("by_date", {}).items():
                if lo <= d <= hi:
                    date_ids.update(ids)
            candidates = date_ids

        if session_id:
            sess_ids = set(idx.get("by_session", {}).get(session_id, []))
            candidates = sess_ids if candidates is None else candidates & sess_ids

        if chat_id:
            chat_ids = set(idx.get("by_chat", {}).get(chat_id, []))
            candidates = chat_ids if candidates is None else candidates & chat_ids

        # If no filter, search all papers
        if candidates is None:
            candidates = {
                p.stem.replace("_", "/")
                for p in self._papers_dir().glob("*.json")
            }

        # Score by keyword match; empty query returns all candidates
        tokens = {t.lower() for t in query.split() if len(t) > 1}
        scored: List[tuple] = []
        for aid in candidates:
            card = self.load_card(aid)
            if not card:
                continue
            if not tokens:
                # No keyword filter — sort by last access date descending
                scored.append((card.last_seen or "0000-00-00", card))
            else:
                haystack = " ".join([
                    card.title,
                    card.tldr,
                    " ".join(card.keywords),
                    card.abstract,
                    " ".join(card.categories),
                ]).lower()
                score = sum(1 for t in tokens if t in haystack)
                if score > 0:
                    scored.append((score, card))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [card for _, card in scored[:limit]]

    def stats(self) -> dict:
        papers_dir = self._papers_dir()
        n_papers = sum(1 for _ in papers_dir.glob("*.json"))
        idx = self._load_index()
        n_dates = len(idx.get("by_date", {}))
        return {"papers_memorised": n_papers, "days_with_activity": n_dates}


# ── Singleton ─────────────────────────────────────────────────────────────────

_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


# ── Date-hint resolver ────────────────────────────────────────────────────────

def resolve_date_range(hint: str) -> tuple[Optional[str], Optional[str]]:
    """
    Convert a natural date hint to (date_from, date_to) ISO strings.

    Accepted formats:
        "2026-02-24"         → exact day
        "yesterday"          → yesterday
        "today"              → today
        "last_week" / "last week"  → Mon–Sun of last week
        "last_month"         → first–last of last month
        "2026-02"            → whole month
    """
    today = date.today()

    h = hint.lower().replace("-", "_").strip()

    if h in ("today",):
        d = today.isoformat()
        return d, d

    if h in ("yesterday",):
        d = (today - timedelta(days=1)).isoformat()
        return d, d

    if h in ("this_week", "thisweek", "recent"):
        # Rolling last-7-days window (today inclusive)
        return (today - timedelta(days=6)).isoformat(), today.isoformat()

    if h in ("last_week", "lastweek"):
        monday = today - timedelta(days=today.weekday() + 7)
        sunday = monday + timedelta(days=6)
        return monday.isoformat(), sunday.isoformat()

    if h in ("last_month", "lastmonth"):
        first_this = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        return last_month_start.isoformat(), last_month_end.isoformat()

    # "2026-02" → whole month
    if len(hint) == 7 and hint[4] == "-":
        try:
            y, m = int(hint[:4]), int(hint[5:7])
            import calendar
            _, last_day = calendar.monthrange(y, m)
            return f"{hint}-01", f"{hint}-{last_day:02d}"
        except ValueError:
            pass

    # Full date "2026-02-24"
    if len(hint) == 10 and hint[4] == "-" and hint[7] == "-":
        return hint, hint

    return None, None
