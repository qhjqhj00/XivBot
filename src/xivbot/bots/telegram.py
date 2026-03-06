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

MENU_STATUS = "ℹ️ Status"
MENU_HELP = "❓ Help"
MENU_SESSIONS = "📚 Sessions"
MENU_NEW_SESSION = "🆕 New Session"
MENU_SWITCH_SESSION = "🔀 Switch Session"
MENU_DELETE_SESSION = "🗑 Delete Session"
MENU_RESET_SESSION = "♻️ Reset Session"
MENU_NOTE = "📝 Note (Last Paper)"
MENU_NOTE_BY_ID = "🆔 Note by arXiv ID"
MENU_DIGEST_TODAY = "📄 Digest Today"
MENU_DIGEST_PERIOD = "🗓 Digest by Period"
MENU_BACKRUN = "🧵 Backrun Task"
MENU_BG_TASKS = "📋 BG Tasks"
MENU_BG_RESULT = "📥 BG Result"
MENU_BG_CANCEL = "🛑 BG Cancel"
MENU_CANCEL_PENDING = "🚪 Cancel Pending"

CB_STATUS = "status"
CB_HELP = "help"
CB_SESSIONS = "sessions"
CB_NEWSESSION = "newsession"
CB_SWITCH = "switch"
CB_DELETE = "deletesession"
CB_RESET = "reset"
CB_NOTE = "note"
CB_NOTE_BY_ID = "note_by_id"
CB_DIGEST_TODAY = "digest_today"
CB_DIGEST_PERIOD = "digest_period"
CB_BACKRUN = "backrun"
CB_BG_TASKS = "bgtasks"
CB_BG_RESULT = "bgresult"
CB_BG_CANCEL = "bgcancel"
CB_CANCEL_PENDING = "cancel_pending"
CB_LANG_SWITCH = "lang_switch"
CB_QUICK_HOME = "quick_home"
CB_QUICK_STATUS = "quick_status"
CB_QUICK_NEWSESSION = "quick_newsession"
CB_SWITCH_PICK_PREFIX = "switch_pick:"


class TelegramBot(CommandsMixin, BotBase):
    """Telegram bot adapter using the raw HTTP Bot API."""

    def __init__(self, bot_token: str, verbose: bool = False):
        super().__init__("Telegram", verbose)
        self._init_commands()
        self.bot_token = bot_token
        self._running = False
        self._offset = 0
        self._pending_menu_input: dict[str, str] = {}
        self._chat_lang: dict[str, str] = {}
        self._http = requests.Session()
        self._http.headers.update({"Content-Type": "application/json"})

    # ── BotBase interface ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._clear_bot_commands()
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
        resp = self._http.get(
            _url(self.bot_token, "getUpdates"),
            params={
                "offset": self._offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ["message", "callback_query"],
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
        callback_query = update.get("callback_query")
        if callback_query:
            self._dispatch_callback(callback_query)
            return

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

        self._ensure_chat_lang(chat_id)

        if text == "/cancel":
            self._pending_menu_input.pop(chat_id, None)

        if text == "/start":
            self._clear_pending(chat_id)
            self._remove_reply_keyboard(chat_id)
            self._send_welcome(chat_id)
            return

        if text in {"/help", "/menu"}:
            self._send_welcome(chat_id)
            return

        if text == "/status":
            self._send(chat_id, self._build_status_localized(chat_id))
            return

        if text == "/sessions":
            if self._lang(chat_id) == "zh":
                self._send_sessions_zh(chat_id)
            else:
                self._dispatch_command(chat_id, text)
            return

        if text == "/bgtasks":
            if self._lang(chat_id) == "zh":
                self._send_bgtasks_zh(chat_id)
            else:
                self._dispatch_command(chat_id, text)
            return

        if text.lower() in {"hi", "hello", "hey", "你好", "嗨", "哈喽"}:
            self._send_welcome(chat_id)
            return

        if self._handle_menu_pending_input(chat_id, text):
            return

        if self._handle_menu_action(chat_id, text):
            return

        text = self._menu_to_command(text)

        # CommandsMixin handles all /commands and pending-state flows
        if self._dispatch_command(chat_id, text):
            return

        # Regular message → agent
        done = threading.Event()

        def _typing_loop() -> None:
            while not done.wait(timeout=4):
                self._send_typing(chat_id)

        def reply(answer: str) -> None:
            done.set()
            self._send(chat_id, answer)

        def _run() -> None:
            self._send_typing(chat_id)
            typing_thread = threading.Thread(target=_typing_loop, daemon=True)
            typing_thread.start()
            try:
                self.on_message(text, reply, chat_id)
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True).start()

    # ── Platform send implementation ──────────────────────────────────────────

    def _send(self, chat_id: str, text: str) -> None:
        plain = _md_to_plain(text)
        chunks = _split(plain, 4096)
        for i, chunk in enumerate(chunks):
            self._post_message(
                chat_id,
                chunk,
                reply_markup=self._quick_menu_markup_for_lang(self._lang(chat_id)) if i == len(chunks) - 1 else None,
            )

    def _send_with_markup(self, chat_id: str, text: str, reply_markup: dict) -> None:
        plain = _md_to_plain(text)
        chunks = _split(plain, 4096)
        for i, chunk in enumerate(chunks):
            self._post_message(chat_id, chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)

    def _send_document(self, chat_id: str, filepath: str, filename: str) -> bool:
        try:
            with open(filepath, "rb") as f:
                resp = self._http.post(
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

    def _send_typing(self, chat_id: str) -> None:
        try:
            self._http.post(
                _url(self.bot_token, "sendChatAction"),
                json={"chat_id": chat_id, "action": "typing"},
                timeout=5,
            )
        except Exception:
            pass

    def _post_message(self, chat_id: str, text: str, reply_markup: Optional[dict] = None) -> bool:
        try:
            payload = {
                "chat_id": chat_id,
                "text": text,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            resp = self._http.post(
                _url(self.bot_token, "sendMessage"),
                json=payload,
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

    def _dispatch_callback(self, callback_query: dict) -> None:
        cb_id = callback_query.get("id")
        data = (callback_query.get("data") or "").strip()
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        username = (
            callback_query.get("from", {}).get("username")
            or callback_query.get("from", {}).get("first_name")
            or chat_id
        )
        if not cb_id or not chat_id:
            return

        self._ensure_chat_lang(chat_id)

        if self.verbose:
            console.log(f"[Telegram] @{username} tapped: {data!r}")

        handled = self._handle_menu_callback(chat_id, data)
        if handled:
            self._answer_callback_query(cb_id)
        else:
            self._answer_callback_query(cb_id, "Unknown menu action.")

    def _answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self._http.post(
                _url(self.bot_token, "answerCallbackQuery"),
                json=payload,
                timeout=10,
            )
        except Exception:
            pass

    def _menu_to_command(self, text: str) -> str:
        command_map = {
            MENU_STATUS: "/status",
            MENU_HELP: "/help",
            MENU_SESSIONS: "/sessions",
            MENU_NEW_SESSION: "/newsession",
            MENU_DELETE_SESSION: "/deletesession",
            MENU_RESET_SESSION: "/reset",
            MENU_NOTE: "/note",
            MENU_DIGEST_TODAY: "/digest today",
            MENU_BG_TASKS: "/bgtasks",
        }
        return command_map.get(text, text)

    def _handle_menu_action(self, chat_id: str, text: str) -> bool:
        if text == MENU_SWITCH_SESSION:
            self._show_switch_menu(chat_id)
            return True

        if text == MENU_NOTE_BY_ID:
            self._pending_menu_input[chat_id] = "note_id"
            self._send(chat_id, "Send the arXiv ID (e.g. 2506.23351).\nSend /cancel to abort.")
            return True

        if text == MENU_DIGEST_PERIOD:
            self._pending_menu_input[chat_id] = "digest_period"
            self._send(
                chat_id,
                "Send digest period/date, e.g. today / this_week / last_week / 2026-02-25.\n"
                "Send /cancel to abort.",
            )
            return True

        if text == MENU_BACKRUN:
            self._pending_menu_input[chat_id] = "backrun"
            self._send(chat_id, "Send the task description for background run.\nSend /cancel to abort.")
            return True

        if text == MENU_BG_RESULT:
            self._dispatch_command(chat_id, "/bgtasks")
            self._pending_menu_input[chat_id] = "bgresult"
            self._send(chat_id, "Reply with task number or task short ID.\nSend /cancel to abort.")
            return True

        if text == MENU_BG_CANCEL:
            self._dispatch_command(chat_id, "/bgtasks")
            self._pending_menu_input[chat_id] = "bgcancel"
            self._send(chat_id, "Reply with task number or task short ID to cancel.\nSend /cancel to abort.")
            return True

        return False

    def _handle_menu_callback(self, chat_id: str, data: str) -> bool:
        if data.startswith(CB_SWITCH_PICK_PREFIX):
            session_id = data[len(CB_SWITCH_PICK_PREFIX):].strip()
            if not session_id:
                self._send(chat_id, self._t(chat_id, "invalid_session"))
                return True
            return self._dispatch_command(chat_id, f"/switch {session_id}")

        command_map = {
            CB_NEWSESSION: "/newsession",
            CB_DELETE: "/deletesession",
            CB_RESET: "/reset",
            CB_NOTE: "/note",
            CB_DIGEST_TODAY: "/digest today",
        }
        if data in command_map:
            return self._dispatch_command(chat_id, command_map[data])

        if data == CB_STATUS:
            self._send(chat_id, self._build_status_localized(chat_id))
            return True

        if data == CB_QUICK_STATUS:
            self._send(chat_id, self._build_status_localized(chat_id))
            return True

        if data == CB_QUICK_HOME:
            self._send_welcome(chat_id)
            return True

        if data == CB_QUICK_NEWSESSION:
            return self._dispatch_command(chat_id, "/newsession")

        if data == CB_SESSIONS:
            if self._lang(chat_id) == "zh":
                self._send_sessions_zh(chat_id)
            else:
                self._dispatch_command(chat_id, "/sessions")
            return True

        if data == CB_BG_TASKS:
            if self._lang(chat_id) == "zh":
                self._send_bgtasks_zh(chat_id)
            else:
                self._dispatch_command(chat_id, "/bgtasks")
            return True

        if data == CB_HELP:
            self._send_welcome(chat_id)
            return True

        if data == CB_SWITCH:
            self._show_switch_menu(chat_id)
            return True

        if data == CB_NOTE_BY_ID:
            self._pending_menu_input[chat_id] = "note_id"
            self._send(chat_id, self._t(chat_id, "prompt_note_id"))
            return True

        if data == CB_DIGEST_PERIOD:
            self._pending_menu_input[chat_id] = "digest_period"
            self._send(chat_id, self._t(chat_id, "prompt_digest"))
            return True

        if data == CB_BACKRUN:
            self._pending_menu_input[chat_id] = "backrun"
            self._send(chat_id, self._t(chat_id, "prompt_backrun"))
            return True

        if data == CB_BG_RESULT:
            self._dispatch_command(chat_id, "/bgtasks")
            self._pending_menu_input[chat_id] = "bgresult"
            self._send(chat_id, self._t(chat_id, "prompt_bgresult"))
            return True

        if data == CB_BG_CANCEL:
            self._dispatch_command(chat_id, "/bgtasks")
            self._pending_menu_input[chat_id] = "bgcancel"
            self._send(chat_id, self._t(chat_id, "prompt_bgcancel"))
            return True

        if data == CB_CANCEL_PENDING:
            self._clear_pending(chat_id)
            self._send(chat_id, self._t(chat_id, "cancelled"))
            return True

        if data == CB_LANG_SWITCH:
            self._chat_lang[chat_id] = "en" if self._lang(chat_id) == "zh" else "zh"
            self._send_welcome(chat_id)
            return True

        return False

    def _handle_menu_pending_input(self, chat_id: str, text: str) -> bool:
        action = self._pending_menu_input.get(chat_id)
        if not action:
            return False

        if text.startswith("/"):
            return False

        payload = text.strip()
        if not payload:
            self._send(chat_id, self._t(chat_id, "empty_input"))
            return True

        if action == "switch":
            self._pending_menu_input.pop(chat_id, None)
            return self._dispatch_command(chat_id, f"/switch {payload}")

        if action == "note_id":
            self._pending_menu_input.pop(chat_id, None)
            return self._dispatch_command(chat_id, f"/note {payload}")

        if action == "digest_period":
            self._pending_menu_input.pop(chat_id, None)
            return self._dispatch_command(chat_id, f"/digest {payload}")

        if action == "backrun":
            self._pending_menu_input.pop(chat_id, None)
            return self._dispatch_command(chat_id, f"/backrun {payload}")

        if action == "bgresult":
            self._pending_menu_input.pop(chat_id, None)
            return self._dispatch_command(chat_id, f"/bgresult {payload}")

        if action == "bgcancel":
            self._pending_menu_input.pop(chat_id, None)
            return self._dispatch_command(chat_id, f"/bgcancel {payload}")

        self._pending_menu_input.pop(chat_id, None)
        return False

    def _inline_menu_markup(self) -> dict:
        return self._inline_menu_markup_for_lang("zh")

    def _quick_menu_markup_for_lang(self, lang: str) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "🏠 欢迎页" if lang == "zh" else "🏠 Home", "callback_data": CB_QUICK_HOME},
                {"text": "ℹ️ 状态" if lang == "zh" else "ℹ️ Status", "callback_data": CB_QUICK_STATUS},
                {"text": "🆕 新建会话" if lang == "zh" else "🆕 New Session", "callback_data": CB_QUICK_NEWSESSION},
            ]]
        }

    def _inline_menu_markup_for_lang(self, lang: str) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": self._menu_label(lang, MENU_STATUS), "callback_data": CB_STATUS},
                    {"text": self._menu_label(lang, MENU_HELP), "callback_data": CB_HELP},
                ],
                [
                    {"text": self._menu_label(lang, MENU_SESSIONS), "callback_data": CB_SESSIONS},
                    {"text": self._menu_label(lang, MENU_NEW_SESSION), "callback_data": CB_NEWSESSION},
                ],
                [
                    {"text": self._menu_label(lang, MENU_SWITCH_SESSION), "callback_data": CB_SWITCH},
                    {"text": self._menu_label(lang, MENU_DELETE_SESSION), "callback_data": CB_DELETE},
                ],
                [
                    {"text": self._menu_label(lang, MENU_RESET_SESSION), "callback_data": CB_RESET},
                    {"text": self._menu_label(lang, MENU_NOTE), "callback_data": CB_NOTE},
                ],
                [
                    {"text": self._menu_label(lang, MENU_NOTE_BY_ID), "callback_data": CB_NOTE_BY_ID},
                    {"text": self._menu_label(lang, MENU_DIGEST_TODAY), "callback_data": CB_DIGEST_TODAY},
                ],
                [
                    {"text": self._menu_label(lang, MENU_DIGEST_PERIOD), "callback_data": CB_DIGEST_PERIOD},
                    {"text": self._menu_label(lang, MENU_BACKRUN), "callback_data": CB_BACKRUN},
                ],
                [
                    {"text": self._menu_label(lang, MENU_BG_TASKS), "callback_data": CB_BG_TASKS},
                    {"text": self._menu_label(lang, MENU_BG_RESULT), "callback_data": CB_BG_RESULT},
                ],
                [
                    {"text": self._menu_label(lang, MENU_BG_CANCEL), "callback_data": CB_BG_CANCEL},
                    {"text": self._menu_label(lang, MENU_CANCEL_PENDING), "callback_data": CB_CANCEL_PENDING},
                ],
                [
                    {"text": self._menu_label(lang, "lang"), "callback_data": CB_LANG_SWITCH},
                ],
            ],
        }

    def _switch_menu_markup(self, chat_id: str) -> dict:
        from ..agent_runner import get_session_manager

        manager = get_session_manager()
        sessions = manager.list_sessions(chat_id)
        active_id = manager.active_session_id(chat_id)

        rows: list[list[dict]] = []
        lang = self._lang(chat_id)
        for i, s in enumerate(sessions[:20], 1):
            marker = "▶ " if s.session_id == active_id else ""
            n_turns = len([m for m in s.messages if m.get("role") == "user"])
            short = s.short_id()
            name = (s.name or ("会话" if lang == "zh" else "Session")).strip().replace("\n", " ")
            if len(name) > 20:
                name = f"{name[:17]}..."
            label = f"{i}. {marker}{name} · {n_turns}t · {short}"
            rows.append([{"text": label, "callback_data": f"{CB_SWITCH_PICK_PREFIX}{s.session_id}"}])

        rows.append([{"text": self._menu_label(lang, MENU_CANCEL_PENDING), "callback_data": CB_CANCEL_PENDING}])
        rows.append([{"text": "⬅️ 返回主菜单" if lang == "zh" else "⬅️ Back to Main Menu", "callback_data": CB_HELP}])
        return {"inline_keyboard": rows}

    def _show_switch_menu(self, chat_id: str) -> None:
        from ..agent_runner import get_session_manager

        manager = get_session_manager()
        sessions = manager.list_sessions(chat_id)
        if not sessions:
            self._send(chat_id, "还没有会话，直接开始提问即可。" if self._lang(chat_id) == "zh" else "No sessions yet. Just start chatting!")
            return

        active_id = manager.active_session_id(chat_id)
        active = next((s for s in sessions if s.session_id == active_id), None)
        active_name = active.name if active else ("无" if self._lang(chat_id) == "zh" else "None")

        self._pending_menu_input.pop(chat_id, None)
        self._send_with_markup(
            chat_id,
            (
                f"请选择要切换到的会话。\n当前会话：{active_name}"
                if self._lang(chat_id) == "zh"
                else f"Select a session to switch.\nCurrent active: {active_name}"
            ),
            self._switch_menu_markup(chat_id),
        )

    def _send_welcome(self, chat_id: str) -> None:
        lang = self._lang(chat_id)
        text = (
            "欢迎使用 XivBot\n\n"
            "按下方按钮直接操作：\n"
            "1) 先点 `📚 Sessions` 看历史会话，或点 `🆕 New Session`\n"
            "2) 直接输入你的研究问题开始对话\n"
            "3) 常用功能：记笔记、生成 Digest、后台任务\n\n"
            "提示：所有按钮流程都支持 `/cancel`。"
            if lang == "zh"
            else
            "Welcome to XivBot\n\n"
            "Use the buttons below to get started:\n"
            "1) Tap `📚 Sessions` to review history, or `🆕 New Session`\n"
            "2) Type any research question to start\n"
            "3) Common actions: notes, digest, background tasks\n\n"
            "Tip: all multi-step flows support `/cancel`."
        )
        self._send_with_markup(chat_id, text, self._inline_menu_markup_for_lang(lang))

    def _remove_reply_keyboard(self, chat_id: str) -> None:
        try:
            self._http.post(
                _url(self.bot_token, "sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": "菜单已切换为对话内按钮。" if self._lang(chat_id) == "zh" else "Menu switched to in-chat buttons.",
                    "reply_markup": {"remove_keyboard": True},
                },
                timeout=10,
            )
        except Exception:
            pass

    def _clear_pending(self, chat_id: str) -> None:
        self._pending_menu_input.pop(chat_id, None)
        self._pending_delete.pop(chat_id, None)
        self._pending_note.pop(chat_id, None)

    def _clear_bot_commands(self) -> None:
        try:
            self._http.post(
                _url(self.bot_token, "setMyCommands"),
                json={"commands": []},
                timeout=10,
            )
        except Exception:
            pass

    def _ensure_chat_lang(self, chat_id: str) -> None:
        self._chat_lang.setdefault(chat_id, "zh")

    def _lang(self, chat_id: str) -> str:
        return self._chat_lang.get(chat_id, "zh")

    def _menu_label(self, lang: str, key: str) -> str:
        zh = {
            MENU_STATUS: "ℹ️ 状态",
            MENU_HELP: "❓ 欢迎页",
            MENU_SESSIONS: "📚 会话",
            MENU_NEW_SESSION: "🆕 新建会话",
            MENU_SWITCH_SESSION: "🔀 切换会话",
            MENU_DELETE_SESSION: "🗑 删除会话",
            MENU_RESET_SESSION: "♻️ 重置会话",
            MENU_NOTE: "📝 记笔记（最近论文）",
            MENU_NOTE_BY_ID: "🆔 按 arXiv ID 记笔记",
            MENU_DIGEST_TODAY: "📄 今日日报",
            MENU_DIGEST_PERIOD: "🗓 按时间生成 Digest",
            MENU_BACKRUN: "🧵 后台任务",
            MENU_BG_TASKS: "📋 任务列表",
            MENU_BG_RESULT: "📥 获取结果",
            MENU_BG_CANCEL: "🛑 取消任务",
            MENU_CANCEL_PENDING: "🚪 取消当前步骤",
            "lang": "🌐 切换到 English",
        }
        en = {
            MENU_STATUS: MENU_STATUS,
            MENU_HELP: MENU_HELP,
            MENU_SESSIONS: MENU_SESSIONS,
            MENU_NEW_SESSION: MENU_NEW_SESSION,
            MENU_SWITCH_SESSION: MENU_SWITCH_SESSION,
            MENU_DELETE_SESSION: MENU_DELETE_SESSION,
            MENU_RESET_SESSION: MENU_RESET_SESSION,
            MENU_NOTE: MENU_NOTE,
            MENU_NOTE_BY_ID: MENU_NOTE_BY_ID,
            MENU_DIGEST_TODAY: MENU_DIGEST_TODAY,
            MENU_DIGEST_PERIOD: MENU_DIGEST_PERIOD,
            MENU_BACKRUN: MENU_BACKRUN,
            MENU_BG_TASKS: MENU_BG_TASKS,
            MENU_BG_RESULT: MENU_BG_RESULT,
            MENU_BG_CANCEL: MENU_BG_CANCEL,
            MENU_CANCEL_PENDING: MENU_CANCEL_PENDING,
            "lang": "🌐 Switch to 中文",
        }
        table = zh if lang == "zh" else en
        return table.get(key, key)

    def _t(self, chat_id: str, key: str) -> str:
        zh = {
            "invalid_session": "无效的会话选择。",
            "prompt_note_id": "请输入 arXiv ID（例如 2506.23351）。\n发送 /cancel 可取消。",
            "prompt_digest": "请输入时间范围或日期，例如 today / this_week / last_week / 2026-02-25。\n发送 /cancel 可取消。",
            "prompt_backrun": "请输入后台任务描述。\n发送 /cancel 可取消。",
            "prompt_bgresult": "请输入任务编号或短 ID。\n发送 /cancel 可取消。",
            "prompt_bgcancel": "请输入要取消的任务编号或短 ID。\n发送 /cancel 可取消。",
            "cancelled": "已取消。",
            "empty_input": "输入为空，请重新输入，或发送 /cancel 取消。",
        }
        en = {
            "invalid_session": "Invalid session selection.",
            "prompt_note_id": "Send the arXiv ID (e.g. 2506.23351).\nSend /cancel to abort.",
            "prompt_digest": "Send digest period/date, e.g. today / this_week / last_week / 2026-02-25.\nSend /cancel to abort.",
            "prompt_backrun": "Send the task description for background run.\nSend /cancel to abort.",
            "prompt_bgresult": "Reply with task number or task short ID.\nSend /cancel to abort.",
            "prompt_bgcancel": "Reply with task number or task short ID to cancel.\nSend /cancel to abort.",
            "cancelled": "Cancelled.",
            "empty_input": "Input is empty, please send again or /cancel.",
        }
        table = zh if self._lang(chat_id) == "zh" else en
        return table.get(key, key)

    def _build_status_localized(self, chat_id: str) -> str:
        text = self._build_status(chat_id)
        if self._lang(chat_id) != "zh":
            return text

        replacements = [
            ("── XivBot Status ──────────────────", "── XivBot 状态 ──────────────────"),
            ("Model     ", "模型      "),
            ("Sessions  ", "会话      "),
            (" total", " 个"),
            ("Memory    ", "记忆      "),
            (" papers", " 篇论文"),
            (" days", " 天"),
            ("Notes     ", "笔记      "),
            (" notes on ", " 条，覆盖 "),
            (" papers", " 篇论文"),
            ("BG Tasks  ", "后台任务  "),
            (" running, ", " 运行中，"),
            (" pending, ", " 排队中，"),
            (" done", " 已完成"),
            ("no tasks yet", "暂无任务"),
            ("Active session", "当前会话"),
            ("No active session yet", "暂无活跃会话"),
            ("Commands", "功能"),
            ("list all sessions", "查看所有会话"),
            ("start a new session", "新建会话"),
            ("switch to session n", "切换到第 n 个会话"),
            ("delete one or more sessions", "删除一个或多个会话"),
            ("clear current session", "清空当前会话"),
            ("add note on last-read paper", "给最近论文添加笔记"),
            ("add note on specific paper", "给指定论文添加笔记"),
            ("generate reading digest (today/this_week/…)", "生成阅读摘要（today/this_week/…）"),
            ("run a long task in the background", "后台运行长任务"),
            ("list background tasks", "查看后台任务"),
            ("get result of a completed task", "获取已完成任务结果"),
            ("cancel a running task", "取消运行中的任务"),
            ("show this panel", "显示此面板"),
            ("full help", "欢迎页与引导"),
            ("Type any research question to get started.", "直接输入任意研究问题开始。"),
            ("turns", "轮"),
        ]
        for old, new in replacements:
            text = text.replace(old, new)
        return text

    def _send_sessions_zh(self, chat_id: str) -> None:
        from ..agent_runner import get_session_manager

        manager = get_session_manager()
        sessions = manager.list_sessions(chat_id)
        active = manager.active_session_id(chat_id)

        if not sessions:
            self._send(chat_id, "还没有会话，直接开始聊天即可。")
            return

        lines = ["你的会话（最新在前）\n"]
        for i, s in enumerate(sessions, 1):
            marker = "▶ " if s.session_id == active else "  "
            n_turns = len([m for m in s.messages if m.get("role") == "user"])
            lines.append(
                f"{marker}{i}. {s.name}\n"
                f"   {s.short_id()} · {n_turns} 轮 · "
                f"{s.updated_at[:16].replace('T', ' ')}"
            )
        lines.append("\n点菜单中的“🔀 切换会话”可直接点选切换。")
        self._send(chat_id, "\n".join(lines))

    def _send_bgtasks_zh(self, chat_id: str) -> None:
        from ..bg_task_store import get_bg_task_store

        tasks = get_bg_task_store().list_tasks(chat_id)
        if not tasks:
            self._send(chat_id, "还没有后台任务。\n可点击“🧵 后台任务”创建。")
            return

        status_zh = {
            "pending": "排队中",
            "running": "运行中",
            "done": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }
        status_icon = {
            "pending": "⏳",
            "running": "🔄",
            "done": "✅",
            "failed": "❌",
            "cancelled": "🚫",
        }

        lines = ["后台任务列表\n"]
        for i, t in enumerate(tasks, 1):
            icon = status_icon.get(t.status, "?")
            status_label = status_zh.get(t.status, t.status)
            ts = ""
            if t.status == "running" and t.started_at:
                ts = f"开始时间: {t.started_at[:16].replace('T', ' ')}"
            elif t.status in ("done", "failed", "cancelled") and t.finished_at:
                ts = f"完成时间: {t.finished_at[:16].replace('T', ' ')}"
            elif t.status == "pending" and t.created_at:
                ts = f"创建时间: {t.created_at[:16].replace('T', ' ')}"

            hint = ""
            if t.status == "done":
                hint = f"  → 点击“📥 获取结果”，输入 {t.short_id()}"
            elif t.status == "failed" and t.error:
                hint = f"\n   错误: {t.error[:80]}"
            elif t.status == "running":
                hint = f"  → 点击“🛑 取消任务”，输入 {t.short_id()}"

            lines.append(
                f"{i}. [{t.short_id()}] {icon} {status_label:<6}  {t.prompt_preview()}\n"
                f"   {ts}{hint}"
            )

        self._send(chat_id, "\n".join(lines))


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
