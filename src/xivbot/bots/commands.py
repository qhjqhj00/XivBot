"""
CommandsMixin — shared command-handling logic for all XivBot platform adapters.

Both TelegramBot and FeishuBot inherit this mixin.  Platform-specific bots only
need to implement:
    _send(chat_id, text)              → send a plain-text message
    _send_document(chat_id, filepath, filename) → send a file; return True/False

Everything else (session management, notes, digest, pending-state flows) lives
here exactly once.
"""
from __future__ import annotations

import re
import threading
from typing import Optional


_GREETINGS = {
    "hi", "hello", "hey", "你好", "嗨", "哈喽", "在吗", "在吗?",
    "hi!", "hello!", "hey!", "howdy", "sup",
}

_HELP_MSG = (
    "XivBot Commands\n\n"
    "/start                – welcome message\n"
    "/status               – current status + active session\n"
    "/help                 – show this help\n"
    "/sessions             – list all your sessions\n"
    "/newsession           – start a fresh session\n"
    "/switch <n>           – switch to session number n\n"
    "/deletesession        – delete sessions (shows list, then pick)\n"
    "/deletesession all    – delete all sessions at once\n"
    "/deletesession 1 3    – delete sessions 1 and 3 directly\n"
    "/reset                – clear current session history\n"
    "/note                 – add a note on the last-read paper\n"
    "/note <arxiv_id>      – add a note on a specific paper\n"
    "/digest               – reading digest for today (returns .md file)\n"
    "/digest this_week     – digest for this week\n"
    "/digest last_week     – digest for last week\n"
    "/digest 2026-02-25    – digest for a specific date\n\n"
    "Saying hi (hi / hello / 你好 …) also shows the status panel.\n\n"
    "Just type any research question and I'll answer it!"
)

_START_MSG = (
    "Hi! I'm XivBot, your arXiv research assistant.\n\n"
    "Ask me anything about papers and I'll search, read, and summarise "
    "the research for you.\n\n"
    "Examples:\n"
    "• What are the latest papers on RAG for medical QA?\n"
    "• Summarise paper 2409.05591\n"
    "• Compare transformer vs Mamba architectures\n\n"
    "/help – show all commands"
)


class CommandsMixin:
    """
    Mixin providing all /command handlers shared across bot platforms.

    Subclasses must implement:
        _send(chat_id: str, text: str) -> None
        _send_document(chat_id: str, filepath: str, filename: str) -> bool
        self.verbose: bool
    """

    def _init_commands(self) -> None:
        """Call from subclass __init__ to initialise mixin state."""
        self._pending_delete: dict[str, list] = {}
        self._pending_note: dict[str, dict] = {}

    # ── Abstract interface (implemented by each platform bot) ─────────────────

    def _send(self, chat_id: str, text: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _send_document(self, chat_id: str, filepath: str, filename: str) -> bool:
        """Default fallback: send file content as plain text."""
        try:
            text = open(filepath, encoding="utf-8").read()
            preview = text[:3000] + ("\n\n…(truncated)" if len(text) > 3000 else "")
            self._send(chat_id, preview)
            return True
        except Exception:
            return False

    # ── Central dispatcher ────────────────────────────────────────────────────

    def _dispatch_command(self, chat_id: str, text: str) -> bool:
        """
        Check if text is a command or a pending-state reply and handle it.
        Returns True if handled (caller should not pass to agent).
        """
        cmd = text.split()[0].lower() if text.startswith("/") else None
        args = text.split()[1:] if cmd else []

        if cmd == "/start":
            self._send(chat_id, _START_MSG)
            return True

        if cmd == "/help":
            self._send(chat_id, _HELP_MSG)
            return True

        if cmd == "/status" or text.lower() in _GREETINGS:
            self._send(chat_id, self._build_status(chat_id))
            return True

        if cmd == "/reset":
            self._handle_reset(chat_id)
            return True

        if cmd == "/newsession":
            self._handle_newsession(chat_id)
            return True

        if cmd == "/sessions":
            self._handle_sessions(chat_id)
            return True

        if cmd == "/switch":
            self._handle_switch(chat_id, args)
            return True

        if cmd == "/deletesession":
            self._handle_deletesession(chat_id, args)
            return True

        if cmd in ("/note", "/notes"):
            self._handle_note(chat_id, args)
            return True

        if cmd == "/digest":
            self._handle_digest(chat_id, args)
            return True

        if cmd == "/cancel":
            self._pending_delete.pop(chat_id, None)
            self._pending_note.pop(chat_id, None)
            self._send(chat_id, "Cancelled.")
            return True

        # Pending two-step flows (only for plain messages, not commands)
        if not cmd:
            if chat_id in self._pending_delete:
                self._handle_delete_confirm(chat_id, text)
                return True
            if chat_id in self._pending_note:
                self._handle_note_text(chat_id, text)
                return True

        return False

    # ── Status ────────────────────────────────────────────────────────────────

    def _build_status(self, chat_id: str) -> str:
        from .. import config as cfg
        from ..agent_runner import get_session_manager
        from ..memory_store import get_memory_store
        from ..note_store import get_note_store

        manager = get_session_manager(verbose=self.verbose)
        llm = cfg.get_llm_config()
        provider_key = llm.get("provider") or "?"
        provider_name = cfg.PROVIDERS.get(provider_key, {}).get("name", provider_key)
        model = llm.get("model") or "?"

        active_id = manager.active_session_id(chat_id)
        sessions = manager.list_sessions(chat_id)

        if active_id:
            active = next((s for s in sessions if s.session_id == active_id), None)
            session_name = active.name if active else "New Session"
            n_turns = len([m for m in (active.messages if active else []) if m["role"] == "user"])
            session_line = f"{session_name}  [{active_id[-6:]}]  ({n_turns} turns)"
        else:
            session_line = "No active session yet"

        mem = get_memory_store().stats()
        notes = get_note_store().stats()

        lines = [
            "── XivBot Status ──────────────────",
            "",
            f"Model     {provider_name} / {model}",
            f"Sessions  {len(sessions)} total",
            f"Memory    {mem['papers_memorised']} papers  ({mem['days_with_activity']} days)",
            f"Notes     {notes['total_notes']} notes on {notes['papers_with_notes']} papers",
            "",
            "Active session",
            f"  {session_line}",
            "",
            "Commands",
            "  /sessions          list all sessions",
            "  /newsession        start a new session",
            "  /switch <n>        switch to session n",
            "  /deletesession     delete one or more sessions",
            "  /reset             clear current session",
            "  /note              add note on last-read paper",
            "  /note <arxiv_id>   add note on specific paper",
            "  /digest [period]   generate reading digest (today/this_week/…)",
            "  /status            show this panel",
            "  /help              full help",
            "",
            "Type any research question to get started.",
        ]
        return "\n".join(lines)

    # ── Session handlers ──────────────────────────────────────────────────────

    def _handle_reset(self, chat_id: str) -> None:
        from ..agent_runner import get_session_manager
        get_session_manager().reset_current(chat_id)
        self._send(chat_id, "Current session cleared. Ask me a new question!")

    def _handle_newsession(self, chat_id: str) -> None:
        from ..agent_runner import get_session_manager
        get_session_manager().new_session(chat_id)
        self._send(chat_id, "New session started.\nAsk your first question!")

    def _handle_sessions(self, chat_id: str) -> None:
        from ..agent_runner import get_session_manager
        manager = get_session_manager()
        sessions = manager.list_sessions(chat_id)
        active = manager.active_session_id(chat_id)

        if not sessions:
            self._send(chat_id, "No sessions yet. Just start chatting!")
            return

        lines = ["Your Sessions (newest first)\n"]
        for i, s in enumerate(sessions, 1):
            marker = "▶ " if s.session_id == active else "  "
            n_turns = len([m for m in s.messages if m["role"] == "user"])
            lines.append(
                f"{marker}{i}. {s.name}\n"
                f"   {s.short_id()} · {n_turns} turns · "
                f"{s.updated_at[:16].replace('T', ' ')}"
            )
        lines.append("\nUse /switch <number> to switch sessions.")
        self._send(chat_id, "\n".join(lines))

    def _handle_switch(self, chat_id: str, args: list) -> None:
        from ..agent_runner import get_session_manager
        manager = get_session_manager()

        if not args:
            self._send(chat_id, "Usage: /switch <number>  (use /sessions to list)")
            return

        sessions = manager.list_sessions(chat_id)
        target_id: Optional[str] = None

        arg = args[0]
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(sessions):
                target_id = sessions[idx].session_id
        else:
            for s in sessions:
                if s.session_id == arg or s.session_id.endswith(arg):
                    target_id = s.session_id
                    break

        if not target_id:
            self._send(chat_id, f"Session not found: {arg}\nUse /sessions to list.")
            return

        if manager.switch_session(chat_id, target_id):
            target = next((s for s in sessions if s.session_id == target_id), None)
            name = target.name if target else target_id
            n_turns = len([m for m in (target.messages if target else []) if m["role"] == "user"])
            self._send(
                chat_id,
                f"Switched to: {name}\n"
                f"{target_id[-6:]} · {n_turns} previous turns restored."
            )
        else:
            self._send(chat_id, "Failed to switch session.")

    # ── Delete session handlers ───────────────────────────────────────────────

    def _handle_deletesession(self, chat_id: str, args: list) -> None:
        from ..agent_runner import get_session_manager
        manager = get_session_manager()
        sessions = manager.list_sessions(chat_id)

        if not sessions:
            self._send(chat_id, "No sessions to delete.")
            return

        active = manager.active_session_id(chat_id)

        if args:
            self._execute_delete(chat_id, args, sessions, active)
            return

        self._pending_delete[chat_id] = sessions
        lines = ["Which sessions do you want to delete?\n"]
        for i, s in enumerate(sessions, 1):
            marker = " [active]" if s.session_id == active else ""
            n_turns = len([m for m in s.messages if m["role"] == "user"])
            lines.append(
                f"{i}. {s.name}{marker}\n"
                f"   {s.short_id()} · {n_turns} turns · "
                f"{s.updated_at[:16].replace('T', ' ')}"
            )
        lines.append("\nReply with numbers (e.g. 1 3) or 'all'.\nSend /cancel to abort.")
        self._send(chat_id, "\n".join(lines))

    def _handle_delete_confirm(self, chat_id: str, text: str) -> None:
        sessions = self._pending_delete.pop(chat_id, None)
        if not sessions:
            return
        from ..agent_runner import get_session_manager
        active = get_session_manager().active_session_id(chat_id)
        self._execute_delete(chat_id, text.split(), sessions, active)

    def _execute_delete(self, chat_id: str, args: list, sessions: list, active: str) -> None:
        from ..agent_runner import get_session_manager
        manager = get_session_manager()

        to_delete: list[str] = []
        if len(args) == 1 and args[0].lower() == "all":
            to_delete = [s.session_id for s in sessions]
        else:
            invalid = []
            for token in args:
                if token.isdigit():
                    idx = int(token) - 1
                    if 0 <= idx < len(sessions):
                        to_delete.append(sessions[idx].session_id)
                    else:
                        invalid.append(token)
                else:
                    invalid.append(token)
            if invalid:
                self._send(
                    chat_id,
                    f"Unknown index(es): {', '.join(invalid)}. "
                    f"Use numbers 1–{len(sessions)} or 'all'."
                )
                return

        to_delete = list(dict.fromkeys(to_delete))
        deleted_names = [s.name for s in sessions if s.session_id in to_delete]
        deleted_active = active in to_delete

        count = manager.delete_sessions(chat_id, to_delete)

        lines = [f"Deleted {count} session(s):"]
        for name in deleted_names:
            lines.append(f"  • {name}")
        if deleted_active:
            lines.append("\nThe active session was deleted. Use /newsession to start fresh.")
        self._send(chat_id, "\n".join(lines))

    # ── Note handlers ─────────────────────────────────────────────────────────

    def _last_arxiv_id_in_session(self, chat_id: str) -> Optional[str]:
        from ..agent_runner import get_session_manager
        from ..session_store import get_store
        session_id = get_session_manager().active_session_id(chat_id)
        if not session_id:
            return None
        session = get_store().load(chat_id, session_id)
        if not session:
            return None
        for msg in reversed(session.messages[-5:]):
            content = msg.get("content") or ""
            m = re.search(r"\b(\d{4}\.\d{4,5})\b", content)
            if m:
                return m.group(1)
        return None

    def _handle_note(self, chat_id: str, args: list) -> None:
        from ..note_store import get_note_store
        from ..memory_store import get_memory_store

        arxiv_id: Optional[str] = None
        title: str = ""

        if args:
            arxiv_id = args[0].strip()
            card = get_memory_store().load_card(arxiv_id)
            title = card.title if card else ""
        else:
            arxiv_id = self._last_arxiv_id_in_session(chat_id)
            if arxiv_id:
                card = get_memory_store().load_card(arxiv_id)
                title = card.title if card else ""
            else:
                self._send(
                    chat_id,
                    "No arXiv ID found in the recent conversation.\n"
                    "Use /note <arxiv_id> to specify a paper directly."
                )
                return

        pnf = get_note_store().get_notes(arxiv_id)
        lines = []
        if pnf.notes:
            lines.append(f"Existing notes for [{arxiv_id}] {title or arxiv_id}:\n")
            for i, n in enumerate(pnf.notes, 1):
                ts = n.created_at[:16].replace("T", " ")
                lines.append(f"{i}. [{ts}]\n{n.content}")
            lines.append("")

        display_title = f"[{arxiv_id}] {title}" if title else arxiv_id
        lines.append(
            f"Send an instruction for {display_title}:\n"
            f"e.g. '记录实验结果' / '总结创新点，200字' / 'summarise contributions'\n"
            f"(or /cancel to abort)"
        )
        self._pending_note[chat_id] = {"arxiv_id": arxiv_id, "title": title}
        self._send(chat_id, "\n".join(lines))

    def _handle_note_text(self, chat_id: str, text: str) -> None:
        info = self._pending_note.pop(chat_id, None)
        if not info:
            return
        instruction = text.strip()
        self._send(chat_id, "Generating note…")
        threading.Thread(
            target=self._generate_and_save_note,
            args=(chat_id, info["arxiv_id"], info.get("title", ""), instruction),
            daemon=True,
        ).start()

    def _generate_and_save_note(
        self, chat_id: str, arxiv_id: str, title: str, instruction: str
    ) -> None:
        try:
            from openai import OpenAI
            from deepxiv_sdk import Reader
            from .. import config as cfg
            from ..note_store import get_note_store

            llm = cfg.get_llm_config()
            client = OpenAI(api_key=llm["api_key"], base_url=llm.get("base_url"))
            reader = Reader(token=cfg.get_deepxiv_token())

            paper_text = ""
            try:
                brief = reader.brief(arxiv_id)
                if brief:
                    kws = brief.get("keywords", [])
                    if isinstance(kws, list):
                        kws = ", ".join(kws)
                    paper_text = (
                        f"Title: {brief.get('title', '')}\n"
                        f"Keywords: {kws}\n"
                        f"TLDR: {brief.get('tldr', '')}\n"
                    )
            except Exception:
                pass
            try:
                preview = reader.preview(arxiv_id)
                if preview:
                    content = preview.get("content") or preview.get("preview") or ""
                    paper_text += "\n" + content[:6000]
            except Exception:
                pass

            if not paper_text.strip():
                paper_text = f"arXiv paper {arxiv_id} (content unavailable)"

            prompt = (
                f"Below is an academic paper.\n\n{paper_text}\n\n---\n"
                f"Instruction: {instruction}\n\n"
                f"Write the note in the same language as the instruction. "
                f"Be concise and accurate."
            )

            resp = client.chat.completions.create(
                model=llm["model"],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.5,
            )
            note_content = resp.choices[0].message.content.strip()

            note_id = get_note_store().add_note(
                arxiv_id=arxiv_id, content=note_content,
                chat_id=chat_id, title=title,
            )
            display = f"[{arxiv_id}] {title}" if title else arxiv_id
            self._send(
                chat_id,
                f"Note saved for {display}  (id: {note_id})\n\n"
                f"── Generated note ──────────────\n"
                f"{note_content}"
            )
        except Exception as exc:
            self._send(chat_id, f"Failed to generate note: {exc}")

    # ── Digest handlers ───────────────────────────────────────────────────────

    def _handle_digest(self, chat_id: str, args: list) -> None:
        date_hint = args[0].strip() if args else "today"
        self._send(chat_id, f"Generating digest for {date_hint}…")
        threading.Thread(
            target=self._run_digest,
            args=(chat_id, date_hint),
            daemon=True,
        ).start()

    def _run_digest(self, chat_id: str, date_hint: str) -> None:
        try:
            from ..note_store import build_digest
            import os
            markdown, filepath = build_digest(date_hint)
            if markdown is None:
                self._send(chat_id, f"No papers found for {date_hint}.")
                return
            filename = os.path.basename(filepath)
            sent = self._send_document(chat_id, filepath, filename)
            if not sent:
                preview = markdown[:3000] + ("\n\n…(truncated)" if len(markdown) > 3000 else "")
                self._send(chat_id, preview)
        except Exception as exc:
            self._send(chat_id, f"Failed to generate digest: {exc}")
