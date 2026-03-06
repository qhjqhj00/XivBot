"""
XivBot Agent Runner.

Runs a ReAct-style loop using the configured LLM + DeepXiv skills.
Provides:
  - run_query()     – single-shot query (used by bot handlers)
  - chat_loop()     – interactive terminal REPL
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from . import config as cfg
from .skills import get_system_prompt, call_skill, get_openai_tools

console = Console()

MAX_TURNS = 20
MAX_TOKENS = 4096
MAX_CONTEXT_MESSAGES = 40  # trim conversation beyond this to prevent context overflow
LLM_MAX_RETRIES = 2
LLM_RETRY_DELAY = 1.0  # seconds


def _llm_call_with_retry(client, retries: int = LLM_MAX_RETRIES, **kwargs):
    """Call client.chat.completions.create with exponential backoff retries."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            last_exc = exc
            err_str = str(exc).lower()
            retryable = any(k in err_str for k in ("timeout", "rate", "429", "502", "503", "connection"))
            if not retryable or attempt == retries:
                raise
            time.sleep(LLM_RETRY_DELAY * (2 ** attempt))
    raise last_exc  # unreachable, but satisfies type checkers


# ── Agent class ───────────────────────────────────────────────────────────────

class XivAgent:
    """
    Lightweight ReAct agent backed by any OpenAI-compatible LLM + DeepXiv skills.
    Supports parallel tool execution when the LLM emits multiple tool calls.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: Optional[str],
        deepxiv_token: Optional[str],
        max_turns: int = MAX_TURNS,
        verbose: bool = False,
        session_id: str = "",
        chat_id: str = "",
    ):
        from openai import OpenAI
        from deepxiv_sdk import Reader

        self.model = model
        self.max_turns = max_turns
        self.verbose = verbose
        self.session_id = session_id
        self.chat_id = chat_id
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.reader = Reader(token=deepxiv_token)
        self.tools = get_openai_tools()
        self._conversation: List[Dict[str, Any]] = []

    # ── public API ───────────────────────────────────────────────────────────

    def query(
        self,
        question: str,
        reset: bool = False,
        cancel_flag: Optional[threading.Event] = None,
    ) -> str:
        if reset:
            self._conversation = []

        self._conversation.append({"role": "user", "content": question})
        self._trim_conversation()

        messages = [
            {"role": "system", "content": get_system_prompt()},
            *self._conversation,
        ]

        for turn in range(self.max_turns):
            if cancel_flag and cancel_flag.is_set():
                answer = "(Task cancelled by user.)"
                self._conversation.append({"role": "assistant", "content": answer})
                return answer

            if self.verbose:
                console.rule(f"[dim]Turn {turn + 1}[/dim]")

            response = _llm_call_with_retry(
                self.client,
                model=self.model,
                messages=messages,
                tools=self.tools,
                tool_choice="auto",
                max_tokens=MAX_TOKENS,
                temperature=0.7,
            )

            msg = response.choices[0].message

            if not msg.tool_calls:
                answer = msg.content or ""
                self._conversation.append({"role": "assistant", "content": answer})
                return answer

            messages.append(msg.model_dump(exclude_unset=True))

            # Execute tool calls in parallel when there are multiple
            tool_results = self._execute_tools_parallel(
                msg.tool_calls, cancel_flag
            )

            if cancel_flag and cancel_flag.is_set():
                answer = "(Task cancelled by user.)"
                self._conversation.append({"role": "assistant", "content": answer})
                return answer

            messages.extend(tool_results)

        # Reached max turns – ask the model to wrap up
        messages.append(
            {
                "role": "user",
                "content": "Please summarise what you've found so far and give a final answer.",
            }
        )
        final_resp = _llm_call_with_retry(
            self.client,
            model=self.model,
            messages=messages,
            max_tokens=MAX_TOKENS,
            temperature=0.7,
        )
        answer = final_resp.choices[0].message.content or ""
        self._conversation.append({"role": "assistant", "content": answer})
        return answer

    def reset(self) -> None:
        self._conversation = []

    # ── Private helpers ───────────────────────────────────────────────────────

    def _execute_tools_parallel(
        self,
        tool_calls: list,
        cancel_flag: Optional[threading.Event],
    ) -> List[Dict[str, Any]]:
        """Execute multiple tool calls concurrently and return results in order."""

        def _run_one(tc):
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            if self.verbose:
                console.print(
                    f"[bold yellow]⚙ Skill:[/bold yellow] [cyan]{fn_name}[/cyan] "
                    f"[dim]{json.dumps(fn_args, ensure_ascii=False)}[/dim]"
                )

            result = call_skill(
                fn_name, fn_args, self.reader,
                session_id=self.session_id,
                chat_id=self.chat_id,
            )

            if self.verbose:
                preview = result[:300] + "…" if len(result) > 300 else result
                console.print(f"[dim]↳ {preview}[/dim]\n")

            return {"role": "tool", "tool_call_id": tc.id, "content": result}

        if len(tool_calls) == 1:
            return [_run_one(tool_calls[0])]

        # Parallel execution for multiple tool calls
        results: Dict[str, Dict] = {}
        with ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as pool:
            futures = {pool.submit(_run_one, tc): tc.id for tc in tool_calls}
            for future in as_completed(futures):
                if cancel_flag and cancel_flag.is_set():
                    break
                tc_id = futures[future]
                try:
                    results[tc_id] = future.result()
                except Exception as exc:
                    results[tc_id] = {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": f"[Skill error] {exc}",
                    }

        # Return in original order
        return [results[tc.id] for tc in tool_calls if tc.id in results]

    def _trim_conversation(self) -> None:
        """Keep conversation within MAX_CONTEXT_MESSAGES to prevent context overflow."""
        if len(self._conversation) <= MAX_CONTEXT_MESSAGES:
            return
        # Keep the first message (initial context) and the most recent messages
        trimmed = self._conversation[-MAX_CONTEXT_MESSAGES:]
        trimmed.insert(0, {
            "role": "system",
            "content": (
                f"[Earlier conversation ({len(self._conversation) - MAX_CONTEXT_MESSAGES} "
                f"messages) was trimmed to save context space.]"
            ),
        })
        self._conversation = trimmed


# ── Terminal chat loop ────────────────────────────────────────────────────────

_COMMANDS = {
    "/help": "Show this help",
    "/reset": "Clear conversation history",
    "/papers": "List papers in context (coming soon)",
    "/exit": "Exit the chat",
    "/quit": "Exit the chat",
}


def chat_loop(verbose: bool = False) -> None:
    """
    Interactive terminal chat REPL.
    Type a research question and XivBot will answer using DeepXiv skills.
    Special commands start with '/'.
    """
    if not cfg.is_configured():
        console.print(
            "[bold red]XivBot is not configured.[/bold red] "
            "Run [bold]xivbot config[/bold] first."
        )
        sys.exit(1)

    llm = cfg.get_llm_config()
    token = cfg.get_deepxiv_token()

    agent = XivAgent(
        api_key=llm["api_key"],
        model=llm["model"],
        base_url=llm.get("base_url"),
        deepxiv_token=token,
        verbose=verbose,
    )

    console.print(
        Panel(
            f"[bold cyan]XivBot[/bold cyan] · Model: [green]{llm['model']}[/green]\n"
            "Ask me anything about arXiv papers.\n"
            "[dim]Type [bold]/help[/bold] for commands, [bold]/exit[/bold] to quit.[/dim]",
            border_style="cyan",
            padding=(0, 2),
        )
    )

    while True:
        try:
            console.print()
            question = console.input("[bold green]You>[/bold green] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye![/dim]")
            break

        if not question:
            continue

        # Handle slash commands
        if question.startswith("/"):
            cmd = question.split()[0].lower()
            if cmd in ("/exit", "/quit"):
                console.print("[dim]Bye![/dim]")
                break
            elif cmd == "/reset":
                agent.reset()
                console.print("[dim]Conversation reset.[/dim]")
                continue
            elif cmd == "/help":
                for c, desc in _COMMANDS.items():
                    console.print(f"  [bold cyan]{c}[/bold cyan]  {desc}")
                continue
            else:
                console.print(f"[dim]Unknown command: {cmd}[/dim]")
                continue

        # Run the query with a spinner
        answer = _run_with_spinner(agent, question, verbose)

        console.print()
        console.print(
            Panel(
                Markdown(answer),
                title="[bold]XivBot[/bold]",
                border_style="blue",
                padding=(1, 2),
            )
        )


def _run_with_spinner(agent: XivAgent, question: str, verbose: bool) -> str:
    if verbose:
        return agent.query(question)

    with console.status("[bold cyan]Thinking…[/bold cyan]", spinner="dots"):
        answer = agent.query(question)
    return answer


# ── Session manager (multi-turn, per chat, with disk persistence) ─────────────

class SessionManager:
    """
    Manages per-chat sessions backed by SessionStore.

    Each "chat_id" (Telegram/Feishu chat) can have multiple named sessions.
    The active session for a chat is tracked in ChatState.
    An in-memory XivAgent is kept alive per active session_id so the
    LangGraph conversation stays hot; it's rebuilt from stored messages
    whenever a session is switched.
    """

    def __init__(self, verbose: bool = False):
        self._verbose = verbose
        self._lock = __import__("threading").Lock()
        # session_id → XivAgent (in-memory, already has history loaded)
        self._agents: Dict[str, XivAgent] = {}

    # ── Agent factory ─────────────────────────────────────────────────────────

    def _make_agent(self, session_id: str = "", chat_id: str = "") -> XivAgent:
        llm = cfg.get_llm_config()
        token = cfg.get_deepxiv_token()
        return XivAgent(
            api_key=llm["api_key"],
            model=llm["model"],
            base_url=llm.get("base_url"),
            deepxiv_token=token,
            verbose=self._verbose,
            session_id=session_id,
            chat_id=chat_id,
        )

    def _load_agent(self, session_id: str, chat_id: str) -> XivAgent:
        """Load or create an agent for a session, restoring history from disk."""
        from .session_store import get_store
        agent = self._make_agent(session_id=session_id, chat_id=chat_id)
        stored = get_store().load(chat_id, session_id)
        if stored:
            agent._conversation = list(stored.messages)
        return agent

    # ── Active session resolution ─────────────────────────────────────────────

    def _get_active_session_id(self, chat_id: str) -> str:
        """
        Return the active session_id for chat_id, creating one if needed.
        """
        from .session_store import get_store, get_chat_state
        store = get_store()
        state = get_chat_state()

        active = state.get_active(chat_id)
        if active and store.load(chat_id, active):
            return active

        # No active session → create a fresh one
        session = store.create(chat_id, name="New Session")
        state.set_active(chat_id, session.session_id)
        return session.session_id

    def _get_agent(self, chat_id: str, session_id: str) -> XivAgent:
        with self._lock:
            if session_id not in self._agents:
                self._agents[session_id] = self._load_agent(session_id, chat_id)
            return self._agents[session_id]

    # ── Public API ────────────────────────────────────────────────────────────

    def query(self, chat_id: str, question: str) -> str:
        """Route a question to the active session for this chat."""
        from .session_store import get_store

        session_id = self._get_active_session_id(chat_id)
        agent = self._get_agent(chat_id, session_id)
        store = get_store()

        try:
            # Persist user message
            store.append_message(chat_id, session_id, "user", question)

            answer = agent.query(question)

            # Persist assistant answer
            store.append_message(chat_id, session_id, "assistant", answer)

            # Auto-name the session after the first real exchange
            session = store.load(chat_id, session_id)
            if session and session.name == "New Session":
                user_turns = [m for m in session.messages if m["role"] == "user"]
                if len(user_turns) == 1:
                    # Fire-and-forget naming in a background thread
                    import threading
                    threading.Thread(
                        target=self._auto_name,
                        args=(chat_id, session_id, question),
                        daemon=True,
                    ).start()

            return answer
        except Exception as exc:
            return f"Agent error: {exc}"

    def new_session(self, chat_id: str) -> str:
        """Start a fresh session and return its session_id."""
        from .session_store import get_store, get_chat_state
        session = get_store().create(chat_id, name="New Session")
        get_chat_state().set_active(chat_id, session.session_id)
        with self._lock:
            self._agents.pop(session.session_id, None)
        return session.session_id

    def switch_session(self, chat_id: str, session_id: str) -> bool:
        """
        Switch to an existing session.
        Returns True on success, False if session not found.
        """
        from .session_store import get_store, get_chat_state
        if not get_store().load(chat_id, session_id):
            return False
        get_chat_state().set_active(chat_id, session_id)
        # Reload agent from disk on next query
        with self._lock:
            self._agents.pop(session_id, None)
        return True

    def reset_current(self, chat_id: str) -> None:
        """Clear the active session's conversation (keep session, wipe messages)."""
        from .session_store import get_store, get_chat_state
        session_id = get_chat_state().get_active(chat_id)
        if not session_id:
            return
        session = get_store().load(chat_id, session_id)
        if session:
            session.messages = []
            session.touch()
            get_store().save(session)
        with self._lock:
            self._agents.pop(session_id, None)

    def list_sessions(self, chat_id: str):
        from .session_store import get_store
        return get_store().list_sessions(chat_id)

    def delete_sessions(self, chat_id: str, session_ids: List[str]) -> int:
        """
        Delete sessions by session_id. If a deleted session is currently active,
        clears the active pointer. Returns the count of sessions actually deleted.
        """
        from .session_store import get_store, get_chat_state
        store = get_store()
        state = get_chat_state()
        active = state.get_active(chat_id)
        deleted = 0
        for sid in session_ids:
            if store.delete(chat_id, sid):
                deleted += 1
                with self._lock:
                    self._agents.pop(sid, None)
        # If the active session was deleted, clear the active pointer
        if active in session_ids:
            state.set_active(chat_id, "")
        return deleted

    def active_session_id(self, chat_id: str) -> Optional[str]:
        from .session_store import get_chat_state
        return get_chat_state().get_active(chat_id)

    # ── LLM-based auto-naming ─────────────────────────────────────────────────

    def _auto_name(self, chat_id: str, session_id: str, first_question: str) -> None:
        """Call LLM to generate a short session name from the first question."""
        try:
            llm = cfg.get_llm_config()
            client = cfg.get_openai_client()
            resp = client.chat.completions.create(
                model=llm["model"],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Give this research conversation a short title (3-6 words, "
                            "no punctuation at the end). The first question was:\n\n"
                            f"{first_question}\n\nTitle:"
                        ),
                    }
                ],
                max_tokens=20,
                temperature=0.5,
            )
            name = resp.choices[0].message.content.strip().strip('"').strip("'")
            if name:
                from .session_store import get_store
                get_store().rename(chat_id, session_id, name)
                if self._verbose:
                    console.log(f"[SessionManager] Auto-named session: {name!r}")
        except Exception as exc:
            if self._verbose:
                console.log(f"[SessionManager] Auto-name failed: {exc}")


# ── Single-shot helper (used by bot handlers) ─────────────────────────────────

_session_manager: Optional["SessionManager"] = None


def get_session_manager(verbose: bool = False) -> "SessionManager":
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager(verbose=verbose)
    elif verbose:
        # Upgrade verbosity if the caller requested it (e.g. xivbot start --verbose)
        # Never downgrade so that a background status call can't silence live logs.
        _session_manager._verbose = True
    return _session_manager


def run_query(question: str, verbose: bool = False, session_id: str = "default") -> str:
    """
    Route a question through the session-aware agent.
    `session_id` here is the chat_id (Telegram/Feishu chat).
    Returns the answer string, or an error message.
    """
    if not cfg.is_configured():
        return "XivBot is not configured. Run `xivbot config` first."

    manager = get_session_manager(verbose=verbose)
    return manager.query(chat_id=session_id, question=question)


# ── Background task runner ────────────────────────────────────────────────────

BG_MAX_TURNS = 40   # Allow more turns for long batch tasks


class BackgroundTaskRunner:
    """
    Runs long agentic tasks in daemon threads, independent of user sessions.

    Each task gets a fresh XivAgent (so it never pollutes session history).
    Results are written to disk as Markdown files and task status is
    updated via BackgroundTaskStore throughout execution.
    """

    def __init__(self, verbose: bool = False):
        self._verbose = verbose
        self._lock = threading.Lock()
        # task_id → threading.Event (cancel flag)
        self._cancel_flags: Dict[str, threading.Event] = {}

    def submit(self, chat_id: str, prompt: str) -> str:
        """
        Create a new background task and start it in a daemon thread.
        Returns the task_id.
        """
        from .bg_task_store import get_bg_task_store

        store = get_bg_task_store()
        task = store.create(chat_id, prompt)
        cancel_flag = threading.Event()
        with self._lock:
            self._cancel_flags[task.task_id] = cancel_flag

        t = threading.Thread(
            target=self._run_task,
            args=(chat_id, task.task_id, prompt, cancel_flag),
            daemon=True,
            name=f"bgtask-{task.task_id[-6:]}",
        )
        t.start()
        return task.task_id

    def cancel(self, chat_id: str, task_id: str) -> bool:
        """
        Signal a running task to cancel at the next tool-call boundary.
        Returns True if a cancel flag was found, False if task not tracked.
        """
        with self._lock:
            flag = self._cancel_flags.get(task_id)
        if flag is None:
            return False
        flag.set()
        return True

    # ── Worker ────────────────────────────────────────────────────────────────

    def _run_task(
        self,
        chat_id: str,
        task_id: str,
        prompt: str,
        cancel_flag: threading.Event,
    ) -> None:
        from .bg_task_store import get_bg_task_store
        from datetime import datetime

        store = get_bg_task_store()
        now = lambda: datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        store.update_status(chat_id, task_id, "running", started_at=now())

        try:
            llm = cfg.get_llm_config()
            token = cfg.get_deepxiv_token()
            agent = XivAgent(
                api_key=llm["api_key"],
                model=llm["model"],
                base_url=llm.get("base_url"),
                deepxiv_token=token,
                max_turns=BG_MAX_TURNS,
                verbose=self._verbose,
                session_id="",
                chat_id=chat_id,
            )

            result = agent.query(prompt, cancel_flag=cancel_flag)

            if cancel_flag.is_set():
                store.update_status(chat_id, task_id, "cancelled", finished_at=now())
            else:
                result_path = store.result_path(chat_id, task_id)
                result_path.write_text(result, encoding="utf-8")
                store.update_status(
                    chat_id, task_id, "done",
                    finished_at=now(),
                    result_file=str(result_path),
                )

        except Exception as exc:
            store.update_status(
                chat_id, task_id, "failed",
                finished_at=now(),
                error=str(exc),
            )
        finally:
            with self._lock:
                self._cancel_flags.pop(task_id, None)


# ── Singleton accessor ────────────────────────────────────────────────────────

_bg_runner: Optional["BackgroundTaskRunner"] = None


def get_bg_runner(verbose: bool = False) -> "BackgroundTaskRunner":
    global _bg_runner
    if _bg_runner is None:
        _bg_runner = BackgroundTaskRunner(verbose=verbose)
    elif verbose:
        _bg_runner._verbose = True
    return _bg_runner
