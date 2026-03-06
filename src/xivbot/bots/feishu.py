"""
Feishu (Lark) bot integration for XivBot.

Uses the Feishu Event Subscription API (v2) with a local HTTP server.
When a user sends a message to the bot, we receive it as a POST to /feishu/event,
process it with the agent, and reply via the Feishu send-message API.

Supports interactive cards (message_card) for inline menus, matching Telegram
feature parity: welcome page, session management, notes, digest, background
tasks, language switching, and localized UI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from typing import Optional

import requests
from rich.console import Console

from .base import BotBase
from .commands import CommandsMixin

console = Console()

_GREETINGS = {
    "hi", "hello", "hey", "你好", "嗨", "哈喽", "在吗", "在吗?",
    "hi!", "hello!", "hey!", "howdy", "sup",
}

# Callback action prefixes / keys (mirroring Telegram's CB_* constants)
ACT_STATUS = "status"
ACT_HELP = "help"
ACT_SESSIONS = "sessions"
ACT_NEWSESSION = "newsession"
ACT_SWITCH = "switch"
ACT_DELETE = "deletesession"
ACT_RESET = "reset"
ACT_NOTE = "note"
ACT_NOTE_BY_ID = "note_by_id"
ACT_DIGEST_TODAY = "digest_today"
ACT_DIGEST_PERIOD = "digest_period"
ACT_BACKRUN = "backrun"
ACT_BG_TASKS = "bgtasks"
ACT_BG_RESULT = "bgresult"
ACT_BG_CANCEL = "bgcancel"
ACT_CANCEL_PENDING = "cancel_pending"
ACT_LANG_SWITCH = "lang_switch"
ACT_SWITCH_PICK_PREFIX = "switch_pick:"


class FeishuBot(CommandsMixin, BotBase):
    """Feishu bot adapter using Flask for the event webhook."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        verification_token: str,
        encrypt_key: Optional[str] = None,
        port: int = 8080,
        verbose: bool = False,
    ):
        super().__init__("Feishu", verbose)
        self._init_commands()
        self.app_id = app_id
        self.app_secret = app_secret
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.port = port
        self._server: Optional[object] = None
        self._tenant_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()
        self._http = requests.Session()
        self._chat_types: dict[str, str] = {}
        self._pending_menu_input: dict[str, str] = {}
        self._chat_lang: dict[str, str] = {}

    # ── Flask app ─────────────────────────────────────────────────────────────

    def _create_flask_app(self):
        try:
            from flask import Flask, request, jsonify
        except ImportError:
            raise RuntimeError(
                "Flask is required for the Feishu bot. "
                "Install it with: pip install flask"
            )

        app = Flask(__name__)
        app.logger.disabled = True

        @app.route("/feishu/event", methods=["POST"])
        def feishu_event():
            raw_body = request.get_data(as_text=True)

            if self.encrypt_key:
                payload = _decrypt_feishu(raw_body, self.encrypt_key)
                if payload is None:
                    return jsonify({"code": 1, "msg": "decrypt failed"}), 400
            else:
                try:
                    payload = json.loads(raw_body)
                except json.JSONDecodeError:
                    return jsonify({"code": 1, "msg": "invalid json"}), 400

            if "challenge" in payload:
                return jsonify({"challenge": payload["challenge"]})

            if not self._verify_token(payload):
                console.log("[Feishu] Token verification failed")
                return jsonify({"code": 1, "msg": "unauthorized"}), 403

            event_type = (
                payload.get("header", {}).get("event_type")
                or payload.get("type")
            )
            if event_type == "im.message.receive_v1":
                self._handle_message_event(payload)
            elif event_type == "card.action.trigger":
                self._handle_card_action(payload)

            return jsonify({"code": 0})

        @app.route("/feishu/card", methods=["POST"])
        def feishu_card_action():
            raw_body = request.get_data(as_text=True)
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                return jsonify({"code": 1, "msg": "invalid json"}), 400
            if "challenge" in payload:
                return jsonify({"challenge": payload["challenge"]})
            self._handle_card_callback(payload)
            return jsonify({})

        @app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok", "bot": "feishu"})

        return app

    # ── Message handling ──────────────────────────────────────────────────────

    def _handle_message_event(self, payload: dict) -> None:
        try:
            event = payload.get("event", {})
            message = event.get("message", {})
            if message.get("message_type", "") != "text":
                return

            content_str = message.get("content", "{}")
            content = json.loads(content_str)
            text = content.get("text", "").strip()

            if "<at" in text:
                text = re.sub(r"<at[^>]*>[^<]*</at>", "", text).strip()

            if not text:
                return

            chat_id = (
                message.get("chat_id")
                or event.get("sender", {}).get("sender_id", {}).get("open_id")
            )
            chat_type = message.get("chat_type", "p2p")
            if chat_id:
                self._chat_types[chat_id] = chat_type

            if self.verbose:
                console.log(f"[Feishu] chat_id={chat_id} text={text!r}")

            session_id = chat_id or "feishu_default"
            self._ensure_chat_lang(session_id)

            self._dispatch_text(session_id, text)

        except Exception as exc:
            console.log(f"[Feishu] Error handling message: {exc}")

    def _dispatch_text(self, chat_id: str, text: str) -> None:
        """Central text dispatch — handles commands, greetings, menu input, agent."""
        if text == "/cancel":
            self._clear_pending(chat_id)
            self._send(chat_id, self._t(chat_id, "cancelled"))
            return

        if text == "/start" or text in {"/help", "/menu"}:
            self._clear_pending(chat_id)
            self._send_welcome(chat_id)
            return

        if text == "/status":
            self._send(chat_id, self._build_status_localized(chat_id))
            return

        if text == "/sessions":
            self._send_sessions_localized(chat_id)
            return

        if text == "/bgtasks":
            self._send_bgtasks_localized(chat_id)
            return

        if text.lower() in _GREETINGS:
            self._send_welcome(chat_id)
            return

        # Handle pending menu input (multi-step flows)
        if self._handle_menu_pending_input(chat_id, text):
            return

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

    # ── Card action callback handling ─────────────────────────────────────────

    def _handle_card_action(self, payload: dict) -> None:
        """Handle card.action.trigger event (v2 card callback)."""
        try:
            event = payload.get("event", {})
            action = event.get("action", {})
            action_value = action.get("value", {})
            act = action_value.get("action", "")

            operator = event.get("operator", {})
            open_id = operator.get("open_id", "")
            chat_id = open_id or "feishu_default"
            self._ensure_chat_lang(chat_id)

            self._route_card_action(chat_id, act)
        except Exception as exc:
            console.log(f"[Feishu] Card action error: {exc}")

    def _handle_card_callback(self, payload: dict) -> None:
        """Handle POST /feishu/card callback (interactive card actions)."""
        try:
            action = payload.get("action", {})
            action_value = action.get("value", {})
            act = action_value.get("action", "")

            open_id = payload.get("open_id", "")
            chat_id = open_id or "feishu_default"
            self._ensure_chat_lang(chat_id)

            self._route_card_action(chat_id, act)
        except Exception as exc:
            console.log(f"[Feishu] Card callback error: {exc}")

    def _route_card_action(self, chat_id: str, act: str) -> None:
        """Route a card button action to the appropriate handler."""
        if not act:
            return

        if act.startswith(ACT_SWITCH_PICK_PREFIX):
            session_id = act[len(ACT_SWITCH_PICK_PREFIX):].strip()
            if session_id:
                self._dispatch_command(chat_id, f"/switch {session_id}")
            else:
                self._send(chat_id, self._t(chat_id, "invalid_session"))
            return

        command_map = {
            ACT_NEWSESSION: "/newsession",
            ACT_DELETE: "/deletesession",
            ACT_RESET: "/reset",
            ACT_NOTE: "/note",
            ACT_DIGEST_TODAY: "/digest today",
        }
        if act in command_map:
            self._dispatch_command(chat_id, command_map[act])
            return

        if act == ACT_STATUS:
            self._send(chat_id, self._build_status_localized(chat_id))
            return

        if act == ACT_HELP:
            self._send_welcome(chat_id)
            return

        if act == ACT_SESSIONS:
            self._send_sessions_localized(chat_id)
            return

        if act == ACT_SWITCH:
            self._show_switch_menu(chat_id)
            return

        if act == ACT_BG_TASKS:
            self._send_bgtasks_localized(chat_id)
            return

        if act == ACT_NOTE_BY_ID:
            self._pending_menu_input[chat_id] = "note_id"
            self._send(chat_id, self._t(chat_id, "prompt_note_id"))
            return

        if act == ACT_DIGEST_PERIOD:
            self._pending_menu_input[chat_id] = "digest_period"
            self._send(chat_id, self._t(chat_id, "prompt_digest"))
            return

        if act == ACT_BACKRUN:
            self._pending_menu_input[chat_id] = "backrun"
            self._send(chat_id, self._t(chat_id, "prompt_backrun"))
            return

        if act == ACT_BG_RESULT:
            self._dispatch_command(chat_id, "/bgtasks")
            self._pending_menu_input[chat_id] = "bgresult"
            self._send(chat_id, self._t(chat_id, "prompt_bgresult"))
            return

        if act == ACT_BG_CANCEL:
            self._dispatch_command(chat_id, "/bgtasks")
            self._pending_menu_input[chat_id] = "bgcancel"
            self._send(chat_id, self._t(chat_id, "prompt_bgcancel"))
            return

        if act == ACT_CANCEL_PENDING:
            self._clear_pending(chat_id)
            self._send(chat_id, self._t(chat_id, "cancelled"))
            return

        if act == ACT_LANG_SWITCH:
            self._chat_lang[chat_id] = "en" if self._lang(chat_id) == "zh" else "zh"
            self._send_welcome(chat_id)
            return

    # ── Menu pending input handling ───────────────────────────────────────────

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

        action_command_map = {
            "switch": "/switch",
            "note_id": "/note",
            "digest_period": "/digest",
            "backrun": "/backrun",
            "bgresult": "/bgresult",
            "bgcancel": "/bgcancel",
        }
        cmd = action_command_map.get(action)
        if cmd:
            self._pending_menu_input.pop(chat_id, None)
            return self._dispatch_command(chat_id, f"{cmd} {payload}")

        self._pending_menu_input.pop(chat_id, None)
        return False

    def _clear_pending(self, chat_id: str) -> None:
        self._pending_menu_input.pop(chat_id, None)
        self._pending_delete.pop(chat_id, None)
        self._pending_note.pop(chat_id, None)

    # ── Welcome & interactive card menus ──────────────────────────────────────

    def _send_welcome(self, chat_id: str) -> None:
        lang = self._lang(chat_id)
        card = self._build_welcome_card(lang)
        self._send_card(chat_id, card)

    def _build_welcome_card(self, lang: str) -> dict:
        if lang == "zh":
            title = "欢迎使用 XivBot"
            desc = (
                "1) 点击下方按钮管理会话、笔记、任务\n"
                "2) 直接输入研究问题开始对话\n"
                "3) 所有多步操作支持发送 /cancel 取消"
            )
        else:
            title = "Welcome to XivBot"
            desc = (
                "1) Use buttons below to manage sessions, notes, tasks\n"
                "2) Type any research question to start\n"
                "3) All multi-step flows support /cancel"
            )

        L = lambda key: self._menu_label(lang, key)  # noqa: E731

        elements = [
            {"tag": "markdown", "content": desc},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                _card_button(L("status"), ACT_STATUS),
                _card_button(L("sessions"), ACT_SESSIONS),
                _card_button(L("newsession"), ACT_NEWSESSION, "primary"),
            ]},
            {"tag": "action", "actions": [
                _card_button(L("switch"), ACT_SWITCH),
                _card_button(L("delete"), ACT_DELETE),
                _card_button(L("reset"), ACT_RESET),
            ]},
            {"tag": "action", "actions": [
                _card_button(L("note"), ACT_NOTE),
                _card_button(L("note_by_id"), ACT_NOTE_BY_ID),
                _card_button(L("digest_today"), ACT_DIGEST_TODAY),
            ]},
            {"tag": "action", "actions": [
                _card_button(L("digest_period"), ACT_DIGEST_PERIOD),
                _card_button(L("backrun"), ACT_BACKRUN),
                _card_button(L("bgtasks"), ACT_BG_TASKS),
            ]},
            {"tag": "action", "actions": [
                _card_button(L("bgresult"), ACT_BG_RESULT),
                _card_button(L("bgcancel"), ACT_BG_CANCEL),
                _card_button(L("cancel"), ACT_CANCEL_PENDING),
            ]},
            {"tag": "action", "actions": [
                _card_button(L("lang"), ACT_LANG_SWITCH),
            ]},
        ]

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": elements,
        }

    def _show_switch_menu(self, chat_id: str) -> None:
        from ..agent_runner import get_session_manager

        manager = get_session_manager()
        sessions = manager.list_sessions(chat_id)
        lang = self._lang(chat_id)

        if not sessions:
            self._send(
                chat_id,
                "还没有会话，直接开始提问即可。" if lang == "zh" else "No sessions yet. Just start chatting!",
            )
            return

        active_id = manager.active_session_id(chat_id)
        active = next((s for s in sessions if s.session_id == active_id), None)
        active_name = active.name if active else ("无" if lang == "zh" else "None")
        title = f"切换会话 · 当前: {active_name}" if lang == "zh" else f"Switch Session · Current: {active_name}"

        actions = []
        for i, s in enumerate(sessions[:20], 1):
            marker = "▶ " if s.session_id == active_id else ""
            n_turns = len([m for m in s.messages if m.get("role") == "user"])
            name = (s.name or ("会话" if lang == "zh" else "Session")).strip().replace("\n", " ")
            if len(name) > 20:
                name = f"{name[:17]}..."
            label = f"{i}. {marker}{name} · {n_turns}{'轮' if lang == 'zh' else 't'} · {s.short_id()}"
            actions.append(
                {"tag": "action", "actions": [
                    _card_button(label, f"{ACT_SWITCH_PICK_PREFIX}{s.session_id}"),
                ]}
            )

        actions.append({"tag": "action", "actions": [
            _card_button(
                "🚪 取消" if lang == "zh" else "🚪 Cancel",
                ACT_CANCEL_PENDING,
            ),
            _card_button(
                "⬅️ 返回主菜单" if lang == "zh" else "⬅️ Back to Menu",
                ACT_HELP,
            ),
        ]})

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green",
            },
            "elements": actions,
        }
        self._send_card(chat_id, card)

    # ── Localized display ─────────────────────────────────────────────────────

    def _send_sessions_localized(self, chat_id: str) -> None:
        if self._lang(chat_id) == "zh":
            self._send_sessions_zh(chat_id)
        else:
            self._dispatch_command(chat_id, "/sessions")

    def _send_bgtasks_localized(self, chat_id: str) -> None:
        if self._lang(chat_id) == "zh":
            self._send_bgtasks_zh(chat_id)
        else:
            self._dispatch_command(chat_id, "/bgtasks")

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
        lines.append("\n发送 /switch <编号> 切换会话，或点击菜单中的"🔀 切换会话"。")
        self._send(chat_id, "\n".join(lines))

    def _send_bgtasks_zh(self, chat_id: str) -> None:
        from ..bg_task_store import get_bg_task_store

        tasks = get_bg_task_store().list_tasks(chat_id)
        if not tasks:
            self._send(chat_id, "还没有后台任务。\n可点击菜单中的"🧵 后台任务"创建。")
            return

        status_zh = {"pending": "排队中", "running": "运行中", "done": "已完成", "failed": "失败", "cancelled": "已取消"}
        status_icon = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "cancelled": "🚫"}

        lines = ["后台任务列表\n"]
        for i, t in enumerate(tasks, 1):
            icon = status_icon.get(t.status, "?")
            status_label = status_zh.get(t.status, t.status)
            ts = ""
            if t.status == "running" and t.started_at:
                ts = f"开始: {t.started_at[:16].replace('T', ' ')}"
            elif t.status in ("done", "failed", "cancelled") and t.finished_at:
                ts = f"完成: {t.finished_at[:16].replace('T', ' ')}"
            elif t.status == "pending" and t.created_at:
                ts = f"创建: {t.created_at[:16].replace('T', ' ')}"

            hint = ""
            if t.status == "done":
                hint = f"  → 发送 /bgresult {t.short_id()} 获取结果"
            elif t.status == "failed" and t.error:
                hint = f"\n   错误: {t.error[:80]}"
            elif t.status == "running":
                hint = f"  → 发送 /bgcancel {t.short_id()} 取消"

            lines.append(
                f"{i}. [{t.short_id()}] {icon} {status_label:<6}  {t.prompt_preview()}\n"
                f"   {ts}{hint}"
            )
        self._send(chat_id, "\n".join(lines))

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

    # ── Language helpers ───────────────────────────────────────────────────────

    def _ensure_chat_lang(self, chat_id: str) -> None:
        self._chat_lang.setdefault(chat_id, "zh")

    def _lang(self, chat_id: str) -> str:
        return self._chat_lang.get(chat_id, "zh")

    def _menu_label(self, lang: str, key: str) -> str:
        zh = {
            "status": "ℹ️ 状态", "help": "❓ 欢迎页",
            "sessions": "📚 会话", "newsession": "🆕 新建会话",
            "switch": "🔀 切换会话", "delete": "🗑 删除会话",
            "reset": "♻️ 重置会话", "note": "📝 记笔记（最近论文）",
            "note_by_id": "🆔 按 ID 记笔记", "digest_today": "📄 今日日报",
            "digest_period": "🗓 按时间 Digest", "backrun": "🧵 后台任务",
            "bgtasks": "📋 任务列表", "bgresult": "📥 获取结果",
            "bgcancel": "🛑 取消任务", "cancel": "🚪 取消当前步骤",
            "lang": "🌐 切换到 English",
        }
        en = {
            "status": "ℹ️ Status", "help": "❓ Help",
            "sessions": "📚 Sessions", "newsession": "🆕 New Session",
            "switch": "🔀 Switch Session", "delete": "🗑 Delete Session",
            "reset": "♻️ Reset Session", "note": "📝 Note (Last Paper)",
            "note_by_id": "🆔 Note by arXiv ID", "digest_today": "📄 Digest Today",
            "digest_period": "🗓 Digest by Period", "backrun": "🧵 Backrun Task",
            "bgtasks": "📋 BG Tasks", "bgresult": "📥 BG Result",
            "bgcancel": "🛑 BG Cancel", "cancel": "🚪 Cancel Pending",
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

    # ── Platform send implementation ──────────────────────────────────────────

    def _send(self, chat_id: str, text: str) -> None:
        token = self._get_tenant_token()
        if not token:
            console.log("[Feishu] Cannot send message: no tenant token")
            return
        chat_type = self._chat_types.get(chat_id, "p2p")
        receive_id_type = "chat_id" if chat_type == "group_chat" else "open_id"
        for chunk in _split_text(text, 3000):
            try:
                resp = self._http.post(
                    f"https://open.feishu.cn/open-apis/im/v1/messages"
                    f"?receive_id_type={receive_id_type}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": chunk}),
                    },
                    timeout=15,
                )
                if self.verbose:
                    console.log(f"[Feishu] send status={resp.status_code}")
            except Exception as exc:
                console.log(f"[Feishu] send error: {exc}")

    def _send_card(self, chat_id: str, card: dict) -> None:
        """Send an interactive card (message_card) to the chat."""
        token = self._get_tenant_token()
        if not token:
            console.log("[Feishu] Cannot send card: no tenant token")
            return
        chat_type = self._chat_types.get(chat_id, "p2p")
        receive_id_type = "chat_id" if chat_type == "group_chat" else "open_id"
        try:
            resp = self._http.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages"
                f"?receive_id_type={receive_id_type}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card),
                },
                timeout=15,
            )
            if self.verbose:
                console.log(f"[Feishu] send_card status={resp.status_code}")
        except Exception as exc:
            console.log(f"[Feishu] send_card error: {exc}")

    def _send_document(self, chat_id: str, filepath: str, filename: str) -> bool:
        token = self._get_tenant_token()
        if not token:
            return False
        try:
            with open(filepath, "rb") as f:
                upload_resp = self._http.post(
                    "https://open.feishu.cn/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": "stream", "file_name": filename},
                    files={"file": (filename, f, "text/markdown")},
                    timeout=30,
                )
            upload_data = upload_resp.json()
            if upload_data.get("code") != 0:
                if self.verbose:
                    console.log(f"[Feishu] file upload error: {upload_data.get('msg')}")
                return False
            file_key = upload_data["data"]["file_key"]

            chat_type = self._chat_types.get(chat_id, "p2p")
            receive_id_type = "chat_id" if chat_type == "group_chat" else "open_id"
            msg_resp = self._http.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages"
                f"?receive_id_type={receive_id_type}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": chat_id,
                    "msg_type": "file",
                    "content": json.dumps({"file_key": file_key}),
                },
                timeout=15,
            )
            result = msg_resp.json()
            if result.get("code") == 0:
                return True
            if self.verbose:
                console.log(f"[Feishu] sendDocument error: {result.get('msg')}")
            return False
        except Exception as exc:
            if self.verbose:
                console.log(f"[Feishu] sendDocument exception: {exc}")
            return False

    # ── Feishu auth ───────────────────────────────────────────────────────────

    def _get_tenant_token(self) -> Optional[str]:
        import time as _time
        with self._token_lock:
            if self._tenant_token and _time.monotonic() < self._token_expires_at:
                return self._tenant_token
            try:
                resp = self._http.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    timeout=10,
                )
                data = resp.json()
                if data.get("code") == 0:
                    self._tenant_token = data["tenant_access_token"]
                    expire_secs = data.get("expire", 7200)
                    self._token_expires_at = _time.monotonic() + max(expire_secs - 300, 60)
                    return self._tenant_token
                console.log(f"[Feishu] Failed to get tenant token: {data.get('msg')}")
            except Exception as exc:
                console.log(f"[Feishu] Token request error: {exc}")
            return None

    def _verify_token(self, payload: dict) -> bool:
        token = (
            payload.get("token")
            or payload.get("header", {}).get("token")
        )
        return token == self.verification_token

    # ── BotBase interface ─────────────────────────────────────────────────────

    def start(self) -> None:
        try:
            from werkzeug.serving import make_server
        except ImportError:
            raise RuntimeError("werkzeug is required (install flask)")
        flask_app = self._create_flask_app()
        console.log(f"[Feishu] Webhook server listening on port {self.port}")
        self._server = make_server("0.0.0.0", self.port, flask_app)
        self._server.serve_forever()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            console.log("[Feishu] Server stopped.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _card_button(text: str, action: str, btn_type: str = "default") -> dict:
    """Build a Feishu interactive card button element."""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": btn_type,
        "value": {"action": action},
    }


def _split_text(text: str, max_len: int = 3000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


def _decrypt_feishu(encrypted_body: str, key: str) -> Optional[dict]:
    try:
        import base64
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend

        data = json.loads(encrypted_body)
        encrypt_bytes = base64.b64decode(data.get("encrypt", ""))
        iv = encrypt_bytes[:16]
        ciphertext = encrypt_bytes[16:]
        key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
        cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        pad = plaintext[-1]
        plaintext = plaintext[:-pad]
        return json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        console.log(f"[Feishu] Decrypt error: {exc}")
        return None
