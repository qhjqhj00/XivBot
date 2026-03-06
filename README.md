# XivBot

A multi-platform research assistant for arXiv papers, powered by the [DeepXiv SDK](https://github.com/deepxiv/deepxiv-sdk).

Ask natural-language research questions in your terminal or through **Telegram / Feishu**, and XivBot searches, reads, and synthesises papers using a ReAct agent loop. It also builds a **personal reading memory**, lets you **take AI-generated notes**, and produces **reading digests** as Markdown files.

> [中文文档](README_zh.md)

---

## Features

| Category | Feature |
|----------|---------|
| **Paper research** | Semantic + keyword search across arXiv and PubMed Central |
| | Full paper reading: metadata, TLDR, section-by-section, full text |
| **Reading memory** | Automatically remembers every paper you read in depth |
| | Recall by topic or date across all sessions (`recall_papers`) |
| **Notes** | `/note` — give an instruction, AI generates and saves the note |
| | Notes stored per-paper in `workspace/notes/` |
| **Digest** | `/digest` — generate a structured Markdown reading digest for any date range |
| | Digest sent as a `.md` file attachment |
| **Background tasks** | `/backrun` — submit a long agentic task that runs in the background |
| | `/bgtasks` — check status of all running / completed tasks |
| | `/bgresult` — retrieve the result as a `.md` file when done |
| **Sessions** | Multi-turn conversation sessions per chat |
| | Create, switch, and soft-delete sessions |
| **Platforms** | Telegram (long-polling, no public URL needed) |
| | Feishu / Lark (webhook, supports file upload) |
| | Interactive terminal REPL |

---

## Quick Start

### 1. Install

```bash
cd XivBot
pip install -e ".[all]"     # installs everything including Telegram + Feishu deps
```

Or install platform extras selectively:

```bash
pip install -e .             # core only (terminal chat)
pip install -e ".[feishu]"   # + Feishu webhook support
pip install -e ".[telegram]" # + Telegram polling support
```

### 2. Configure

```bash
xivbot config
```

The wizard guides you through:

| Step | What | Notes |
|------|------|-------|
| 1 | LLM provider + API key | openai / deepseek / claude / xai / zhipu / minimax / kimi |
| 2 | DeepXiv API key | Free at <https://data.rag.ac.cn/register> |
| 3 | Bot platforms | Feishu and/or Telegram (optional) |

Reconfigure individual sections any time:

```bash
xivbot config --llm-only
xivbot config --deepxiv-only
xivbot config --bots-only
```

### 3. Use in the terminal

```bash
xivbot chat                  # interactive multi-turn REPL
xivbot chat --verbose        # show agent reasoning + tool calls
xivbot ask "What are the best RAG papers in 2024?"
```

### 4. Start bot service

```bash
xivbot start                 # start all enabled bots (Telegram + Feishu)
xivbot start --verbose       # show live logs of every query and tool call
xivbot start --telegram      # override: start Telegram only
xivbot start --feishu        # override: start Feishu only
```

### 5. Check status

```bash
xivbot status                # show config, session count, memory stats
```

---

## Bot Commands

The same commands work on both Telegram and Feishu:

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Full command reference |
| `/status` | Current config, session info, memory stats |
| `/sessions` | List all sessions (newest first) |
| `/newsession` | Start a fresh session |
| `/switch <n>` | Switch to session number `n` |
| `/deletesession` | Interactive: shows list, then delete by number(s) or `all` |
| `/deletesession 1 3` | Delete sessions 1 and 3 directly |
| `/deletesession all` | Delete all sessions |
| `/reset` | Clear the current session's conversation history |
| `/note` | Add an AI-generated note on the last paper in the session |
| `/note <arxiv_id>` | Add a note on a specific paper |
| `/digest` | Generate a reading digest for **today** (returns `.md` file) |
| `/digest this_week` | Digest for the last 7 days |
| `/digest last_week` | Digest for the previous calendar week |
| `/digest last_month` | Digest for last month |
| `/digest 2026-02-25` | Digest for a specific date |
| `/cancel` | Cancel any pending two-step command |

Greeting words (`hi`, `hello`, `你好`, …) also show the status panel.

### Telegram Menu Mode (new)

Telegram now uses an in-chat inline menu on the welcome panel. Regular replies are text-only by default, and slash commands are still supported for power users.

Use `/start` (or say `hi`, or `/help`) to open the welcome panel with quick operation guidance and menu buttons.

Each normal bot reply also includes a one-line quick menu:
- `🏠 Home`  `ℹ️ Status`  `🆕 New Session`

So you can jump back to the welcome panel, check status, or start a fresh session anytime.

| Menu Button | Equivalent command / behavior |
|------------|-------------------------------|
| `ℹ️ Status` | `/status` |
| `❓ Help` | open the welcome panel |
| `📚 Sessions` | `/sessions` |
| `🆕 New Session` | `/newsession` |
| `🔀 Switch Session` | opens a dedicated session-only menu with session details; tap one to switch |
| `🗑 Delete Session` | `/deletesession` (follow prompt) |
| `♻️ Reset Session` | `/reset` |
| `📝 Note (Last Paper)` | `/note` |
| `🆔 Note by arXiv ID` | prompts for arXiv ID (`/note <arxiv_id>`) |
| `📄 Digest Today` | `/digest today` |
| `🗓 Digest by Period` | prompts for period/date (`/digest <period>`) |
| `🧵 Backrun Task` | prompts for task description (`/backrun <task>`) |
| `📋 BG Tasks` | `/bgtasks` |
| `📥 BG Result` | prompts for task number/short ID (`/bgresult <n>`) |
| `🛑 BG Cancel` | prompts for task number/short ID (`/bgcancel <n>`) |
| `🚪 Cancel Pending` | cancel pending multi-step input (same effect as `/cancel`) |
| `🌐 Switch to 中文 / English` | switch the menu and prompts language immediately |

All menu-driven multi-step flows can be cancelled with `/cancel`.
The status panel language also follows your selected menu language.
In Chinese mode, `Sessions` and `BG Tasks` list pages are also localized.

---

## Background Tasks

For long, multi-step agentic tasks (e.g. surveying dozens of papers and writing notes), use `/backrun` to run the task in the background while you continue chatting.

```
User:  /backrun 帮我调研 50 篇今年的 agentic memory paper，全部写好笔记，最后给我 md

Bot:   Background task started  [id: a1b2c3]
       Use /bgtasks to check status.
       Use /bgresult a1b2c3 to get the result when done.
```

Check progress at any time:

```
User:  /bgtasks

Bot:   Background Tasks

       1. [a1b2c3] 🔄 running     帮我调研 50 篇今年的 agentic memory...
            started: 2026-02-25 14:30  → /bgcancel a1b2c3 to cancel
```

Once complete, retrieve the result as a `.md` file:

```
User:  /bgresult 1

Bot:   📎 20260225_143000_a1b2c3_result.md
```

| Command | Description |
|---------|-------------|
| `/backrun <task>` | Start a long agentic task in the background |
| `/bgtasks` | List all background tasks with status (pending / running / done / failed) |
| `/bgresult <n>` | Get the result of task number `n` or by short ID (sends `.md` file) |
| `/bgcancel <n>` | Cancel a running task at the next tool-call boundary |

Background tasks each get a **fresh agent** (isolated from your active session) with up to **40 tool-call turns**, suitable for batch research workflows.

---

## Agent Skills

The LLM can call these tools automatically during a conversation:

| Skill | Description |
|-------|-------------|
| `search_papers` | Semantic + keyword hybrid search across arXiv |
| `get_paper_metadata` | Full metadata: title, authors, abstract, categories, sections |
| `get_paper_brief` | Fast TLDR + keywords + citation count |
| `get_paper_preview` | First ~10 000 chars of paper text |
| `read_paper_section` | Named section (Introduction, Methods, Conclusion, …) |
| `get_full_paper` | Full paper markdown |
| `get_pmc_metadata` | PubMed Central paper metadata |
| `get_pmc_full` | Full PMC paper content |
| `batch_paper_briefs` | Quick briefs for multiple papers at once |
| `recall_papers` | Search personal reading history by topic and/or date |
| `read_paper_notes` | Read saved notes for a paper |
| `list_noted_papers` | List all papers that have saved notes |

### Reading memory & recall

XivBot silently memorises every paper you read in depth (via `get_paper_brief`, `get_paper_preview`, `read_paper_section`, etc.) into `workspace/memory/`. Each memory card stores the paper's title, TLDR, keywords, arXiv categories, and access timestamps.

When you ask things like:

- *"那几篇关于具身智能的论文"*
- *"我最近看了什么 paper"*
- *"papers I read this week on RAG"*

The agent calls `recall_papers` first. If a date or topic filter yields no results it automatically expands the search so you always get an answer.

### Notes

```
User:  /note
Bot:   Existing notes for [2506.23351] RoboTwin…
       Send an instruction (e.g. '总结创新点，200字' / 'summarise contributions'):

User:  总结这篇论文的实验结果，200字以内

Bot:   ✅ Note saved (id: a1b2c3d4)
       ── Generated note ──────────────
       本文实验在 RoboTwin 基准上评估了…
```

Notes are stored in `workspace/notes/<arxiv_id>.json`.

### Reading digest

```
User:  /digest today
Bot:   Generating digest for today…
       📎 digest_today_2026-02-25.md
```

The digest is a structured Markdown document with:
- Overview paragraph
- Per-paper section with summary and any saved notes
- Key Takeaways section synthesising themes across all papers

Digests are also saved to `workspace/digests/`.

---

## Workspace Layout

```
~/.xivbot/config.json          ← credentials and settings

<workspace>/                   ← default: ~/xivbot_workspace/
├── sessions/
│   └── <chat_id>/
│       ├── <session_id>.json  ← conversation history per session
│       └── _state.json        ← active session pointer
├── memory/
│   ├── index.json             ← by_date / by_session index
│   └── papers/
│       └── <arxiv_id>.json    ← memory card per paper
├── notes/
│   └── <arxiv_id>.json        ← user notes per paper
├── digests/
│   └── digest_today_2026-02-25.md
└── bg_tasks/
    └── <chat_id>/
        ├── <task_id>.json     ← task metadata (status, timestamps, prompt)
        └── <task_id>_result.md ← final result when done
```

---

## Platform Setup

### Telegram

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the bot token
3. Run `xivbot config --bots-only` and enter the token

XivBot uses **long-polling** — no public URL or server setup required.

### Feishu / Lark

1. Go to [open.feishu.cn](https://open.feishu.cn) → Create a custom app
2. Enable **Bot** capability under *Add Capabilities*
3. Subscribe to the `im.message.receive_v1` event
4. Set the Event Callback URL to `http://<your-server>:<port>/feishu/event`
5. Run `xivbot config --bots-only` and enter App ID, App Secret, Verification Token

The bot listens on `0.0.0.0:<port>` (default 8080). If your server is behind NAT, use [ngrok](https://ngrok.com) or a reverse proxy.

---

## Supported LLM Providers

| Key | Provider | Example Models |
|-----|----------|---------------|
| `openai` | OpenAI | gpt-4o, gpt-4o-mini |
| `claude` | Anthropic | claude-3-5-sonnet-20241022 |
| `deepseek` | DeepSeek | deepseek-chat, deepseek-reasoner |
| `xai` | xAI (Grok) | grok-2-1212 |
| `zhipu` | ZhipuAI | glm-4-plus, glm-4-air |
| `minimax` | MiniMax | abab6.5s-chat |
| `kimi` | Moonshot AI | moonshot-v1-32k |

All providers use an OpenAI-compatible API — switching providers only requires updating `~/.xivbot/config.json`.

You can also use [OpenRouter](https://openrouter.ai) as a unified gateway to access any model.

---

## Architecture & Performance

| Optimization | Description |
|---|---|
| **Parallel tool execution** | When the LLM emits multiple tool calls in one turn, they execute concurrently (ThreadPoolExecutor, up to 4 workers) |
| **Batch paper briefs** | `batch_paper_briefs` fetches up to 10 papers concurrently instead of sequentially |
| **LLM retry with backoff** | Transient errors (timeout, rate-limit, 502/503) are retried up to 2 times with exponential backoff |
| **Conversation trimming** | Conversations beyond 40 messages are automatically trimmed to prevent context overflow |
| **Config caching** | Config file is cached in memory with 5s TTL — no disk read on every API call |
| **HTTP connection pooling** | Telegram and Feishu bots reuse `requests.Session` for all HTTP calls |
| **Feishu token caching** | Tenant access token is cached until near-expiry instead of re-fetched per message |
| **Session store caching** | Active sessions are cached in memory (LRU, up to 32) to avoid redundant disk I/O |
| **Centralized OpenAI client** | A single cached OpenAI client is shared across auto-naming, note generation, and digest |

---

## Configuration File

`~/.xivbot/config.json` (mode 0600):

```json
{
  "llm": {
    "provider": "deepseek",
    "api_key": "sk-...",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1"
  },
  "deepxiv": {
    "api_key": "dx-..."
  },
  "bots": {
    "telegram": {
      "enabled": true,
      "bot_token": "123456789:AAH..."
    },
    "feishu": {
      "enabled": false,
      "app_id": "cli_...",
      "app_secret": "...",
      "verification_token": "...",
      "encrypt_key": null,
      "port": 8080
    }
  }
}
```
