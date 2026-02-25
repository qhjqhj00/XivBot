"""
Feishu (Lark) bot integration for XivBot.

Uses the Feishu Event Subscription API (v2) with a local HTTP server.
When a user sends a message to the bot, we receive it as a POST to /feishu/event,
process it with the agent, and reply via the Feishu send-message API.

Setup required (in Feishu Developer Console):
  • Create a custom app, enable Bot capability
  • Subscribe to event: im.message.receive_v1
  • Set Event Callback URL to http://<host>:<port>/feishu/event
  • Copy App ID, App Secret, and Verification Token
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
        self._token_lock = threading.Lock()
        # chat_id → chat_type (needed for _send to pick receive_id_type)
        self._chat_types: dict[str, str] = {}

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

            return jsonify({"code": 0})

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

            # Strip @bot mention
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

            # CommandsMixin handles /commands and pending flows
            if self._dispatch_command(session_id, text):
                return

            # Regular message → agent
            def reply(answer: str) -> None:
                self._send(session_id, answer)

            threading.Thread(
                target=self.on_message,
                args=(text, reply, session_id),
                daemon=True,
            ).start()

        except Exception as exc:
            console.log(f"[Feishu] Error handling message: {exc}")

    # ── Platform send implementation ──────────────────────────────────────────

    def _send(self, chat_id: str, text: str) -> None:
        """Send a plain-text message. Splits at 3000-char Feishu limit."""
        token = self._get_tenant_token()
        if not token:
            console.log("[Feishu] Cannot send message: no tenant token")
            return
        chat_type = self._chat_types.get(chat_id, "p2p")
        receive_id_type = "chat_id" if chat_type == "group_chat" else "open_id"
        for chunk in _split_text(text, 3000):
            try:
                resp = requests.post(
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

    def _send_document(self, chat_id: str, filepath: str, filename: str) -> bool:
        """
        Upload file via Feishu im/v1/files and send as a file message.
        Falls back to text if upload fails.
        """
        token = self._get_tenant_token()
        if not token:
            return False
        try:
            # Step 1: upload file
            with open(filepath, "rb") as f:
                upload_resp = requests.post(
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

            # Step 2: send file message
            chat_type = self._chat_types.get(chat_id, "p2p")
            receive_id_type = "chat_id" if chat_type == "group_chat" else "open_id"
            msg_resp = requests.post(
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
        with self._token_lock:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                self._tenant_token = data["tenant_access_token"]
                return self._tenant_token
            console.log(f"[Feishu] Failed to get tenant token: {data.get('msg')}")
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


# ── Utilities ─────────────────────────────────────────────────────────────────

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
