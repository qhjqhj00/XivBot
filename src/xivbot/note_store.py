"""
XivBot Paper Note Store.

Stores user-written notes (annotations, comments) per arXiv paper.

Disk layout:
    <workspace>/notes/
    └── {arxiv_id}.json   ← all notes for that paper

Each file:
    {
      "arxiv_id":  "2506.23351",
      "title":     "...",
      "notes": [
        {
          "id":         "uuid hex",
          "content":    "User's note text",
          "created_at": "2026-02-25T15:00:00",
          "chat_id":    "8376142125"
        },
        ...
      ]
    }
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import config as cfg


# ── Data model ────────────────────────────────────────────────────────────────

class PaperNote:
    def __init__(
        self,
        note_id: str,
        content: str,
        created_at: str,
        chat_id: str = "",
    ):
        self.note_id = note_id
        self.content = content
        self.created_at = created_at
        self.chat_id = chat_id

    def to_dict(self) -> dict:
        return {
            "id": self.note_id,
            "content": self.content,
            "created_at": self.created_at,
            "chat_id": self.chat_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PaperNote":
        return cls(
            note_id=d["id"],
            content=d["content"],
            created_at=d.get("created_at", ""),
            chat_id=d.get("chat_id", ""),
        )


class PaperNoteFile:
    """All notes for a single paper."""

    def __init__(self, arxiv_id: str, title: str = "", notes: Optional[List[PaperNote]] = None):
        self.arxiv_id = arxiv_id
        self.title = title
        self.notes: List[PaperNote] = notes or []

    def to_dict(self) -> dict:
        return {
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "notes": [n.to_dict() for n in self.notes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PaperNoteFile":
        return cls(
            arxiv_id=d["arxiv_id"],
            title=d.get("title", ""),
            notes=[PaperNote.from_dict(n) for n in d.get("notes", [])],
        )


# ── Store ─────────────────────────────────────────────────────────────────────

class NoteStore:
    """Thread-safe note store backed by JSON files."""

    def __init__(self):
        self._lock = threading.Lock()

    def _notes_dir(self) -> Path:
        d = cfg.get_workspace_dir() / "notes"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _note_path(self, arxiv_id: str) -> Path:
        safe = arxiv_id.replace("/", "_")
        return self._notes_dir() / f"{safe}.json"

    def _load(self, arxiv_id: str) -> PaperNoteFile:
        p = self._note_path(arxiv_id)
        if not p.exists():
            return PaperNoteFile(arxiv_id=arxiv_id)
        try:
            with open(p, "r", encoding="utf-8") as f:
                return PaperNoteFile.from_dict(json.load(f))
        except Exception:
            return PaperNoteFile(arxiv_id=arxiv_id)

    def _save(self, pnf: PaperNoteFile) -> None:
        with open(self._note_path(pnf.arxiv_id), "w", encoding="utf-8") as f:
            json.dump(pnf.to_dict(), f, ensure_ascii=False, indent=2)

    # ── Public API ────────────────────────────────────────────────────────────

    def add_note(
        self,
        arxiv_id: str,
        content: str,
        chat_id: str = "",
        title: str = "",
    ) -> str:
        """Save a note. Returns the new note_id."""
        note = PaperNote(
            note_id=uuid.uuid4().hex[:8],
            content=content,
            created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            chat_id=chat_id,
        )
        with self._lock:
            pnf = self._load(arxiv_id)
            if title and not pnf.title:
                pnf.title = title
            pnf.notes.append(note)
            self._save(pnf)
        return note.note_id

    def get_notes(self, arxiv_id: str) -> PaperNoteFile:
        """Return the note file for a paper (empty if none)."""
        return self._load(arxiv_id)

    def delete_note(self, arxiv_id: str, note_id: str) -> bool:
        """Remove a single note by id. Returns True if found."""
        with self._lock:
            pnf = self._load(arxiv_id)
            before = len(pnf.notes)
            pnf.notes = [n for n in pnf.notes if n.note_id != note_id]
            if len(pnf.notes) == before:
                return False
            self._save(pnf)
        return True

    def list_noted_papers(self) -> List[Dict]:
        """Return brief info for all papers that have at least one note."""
        result = []
        for p in sorted(self._notes_dir().glob("*.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                arxiv_id = d.get("arxiv_id", p.stem.replace("_", "/"))
                n = len(d.get("notes", []))
                if n > 0:
                    result.append({
                        "arxiv_id": arxiv_id,
                        "title": d.get("title", ""),
                        "note_count": n,
                        "last_note": d["notes"][-1]["created_at"] if d.get("notes") else "",
                    })
            except Exception:
                continue
        result.sort(key=lambda x: x["last_note"], reverse=True)
        return result

    def stats(self) -> dict:
        noted = self.list_noted_papers()
        total_notes = sum(p["note_count"] for p in noted)
        return {"papers_with_notes": len(noted), "total_notes": total_notes}


# ── Digest builder ────────────────────────────────────────────────────────────

def build_digest(date_hint: str) -> "tuple[str, str] | tuple[None, None]":
    """
    Generate a Markdown digest of papers read (and optionally noted) within
    the given date window.

    Returns (markdown_text, filepath) or (None, None) if no papers found.
    """
    from datetime import date as _date
    from .memory_store import get_memory_store, resolve_date_range
    from . import config as cfg

    mem_store = get_memory_store()
    note_store = get_note_store()

    date_from, date_to = resolve_date_range(date_hint) if date_hint else (None, None)
    papers = mem_store.recall(query="", date_from=date_from, date_to=date_to)
    if not papers:
        return None, None

    # ── Assemble per-paper context ────────────────────────────────────────────
    sections: List[dict] = []
    for p in papers:
        pnf = note_store.get_notes(p.arxiv_id)
        sections.append({
            "arxiv_id": p.arxiv_id,
            "title":    p.title or p.arxiv_id,
            "tldr":     p.tldr,
            "keywords": p.keywords,
            "notes":    [n.content for n in pnf.notes],
        })

    # ── Build LLM prompt ──────────────────────────────────────────────────────
    today_str = _date.today().isoformat()
    date_label = date_hint or "all time"

    paper_blocks: List[str] = []
    for s in sections:
        block = f"### [{s['arxiv_id']}] {s['title']}\n"
        if s["keywords"]:
            block += f"Keywords: {', '.join(s['keywords'][:6])}\n"
        if s["tldr"]:
            block += f"TLDR: {s['tldr'][:500]}\n"
        if s["notes"]:
            block += "User notes:\n"
            for i, n_text in enumerate(s["notes"], 1):
                block += f"  {i}. {n_text}\n"
        paper_blocks.append(block)

    prompt = (
        f"Today is {today_str}. The user has been reading the following arXiv papers "
        f"({date_label}).\n\n"
        + "\n\n".join(paper_blocks)
        + "\n\n---\n"
        f"Please produce a well-structured Markdown reading digest. Requirements:\n"
        f"1. Start with a `# Reading Digest — {date_label}` heading and a brief one-paragraph overview.\n"
        f"2. For each paper, write a `## [arxiv_id] Title` section containing:\n"
        f"   - A concise summary of what the paper is about.\n"
        f"   - If the user wrote notes, incorporate them or add a **My Notes** subsection quoting them verbatim.\n"
        f"3. End with a `## Key Takeaways` section synthesising the main themes across all papers.\n"
        f"4. Write in the same language as the user notes if present, otherwise use English.\n"
        f"5. Output clean Markdown only — no extra commentary outside the document."
    )

    # ── Call LLM ──────────────────────────────────────────────────────────────
    llm = cfg.get_llm_config()
    client = cfg.get_openai_client()
    resp = client.chat.completions.create(
        model=llm["model"],
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
        temperature=0.5,
    )
    markdown = resp.choices[0].message.content.strip()

    # ── Save to workspace/digests/ ────────────────────────────────────────────
    digests_dir = cfg.get_workspace_dir() / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)
    safe_hint = (date_hint or "all").replace(" ", "_")
    filename = f"digest_{safe_hint}_{today_str}.md"
    filepath = digests_dir / filename
    filepath.write_text(markdown, encoding="utf-8")

    return markdown, str(filepath)


# ── Singleton ─────────────────────────────────────────────────────────────────

_store: Optional[NoteStore] = None


def get_note_store() -> NoteStore:
    global _store
    if _store is None:
        _store = NoteStore()
    return _store
