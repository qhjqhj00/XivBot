# XivBot

基于 [DeepXiv SDK](https://github.com/deepxiv/deepxiv-sdk) 的多平台 arXiv 论文研究助手。

在终端或通过 **Telegram / 飞书** 用自然语言提问，XivBot 会自动搜索、阅读论文并综合答案。它还会构建**个人阅读记忆**、支持 **AI 生成笔记**，并能生成 Markdown 格式的**阅读摘要文件**。

> [English README](README.md)

---

## 功能特性

| 分类 | 功能 |
|------|------|
| **论文搜索与阅读** | 语义 + 关键词混合搜索 arXiv 和 PubMed Central |
| | 多层次阅读：元数据、TLDR、逐章节、全文 |
| **阅读记忆** | 自动记忆你深度阅读过的每一篇论文 |
| | 按主题或日期跨 session 召回（`recall_papers`） |
| **笔记** | `/note` — 发一条指令，AI 自动生成并保存笔记 |
| | 笔记按论文存储于 `workspace/notes/` |
| **阅读摘要** | `/digest` — 按时间维度生成结构化 Markdown 摘要文件 |
| | 以 `.md` 附件形式发送 |
| **后台任务** | `/backrun` — 将长时间的 Agent 任务提交到后台运行 |
| | `/bgtasks` — 查看所有后台任务的状态 |
| | `/bgresult` — 任务完成后获取 `.md` 结果文件 |
| **多会话管理** | 每个聊天支持多个命名会话 |
| | 创建、切换、软删除会话 |
| **多平台** | Telegram（长轮询，无需公网地址） |
| | 飞书 / Lark（Webhook，支持文件发送） |
| | 终端交互式对话 |

---

## 快速开始

### 1. 安装

```bash
cd XivBot
pip install -e ".[all]"     # 安装所有依赖，包括 Telegram 和飞书支持
```

也可以按需选择：

```bash
pip install -e .             # 仅核心（终端对话）
pip install -e ".[feishu]"   # + 飞书 Webhook 支持
pip install -e ".[telegram]" # + Telegram 支持
```

### 2. 配置

```bash
xivbot config
```

向导会引导你完成：

| 步骤 | 内容 | 说明 |
|------|------|------|
| 1 | LLM 提供商 + API Key | openai / deepseek / claude / xai / zhipu / minimax / kimi |
| 2 | DeepXiv API Key | 免费注册：<https://data.rag.ac.cn/register> |
| 3 | Bot 平台 | 飞书 和/或 Telegram（可选） |

随时重新配置某一项：

```bash
xivbot config --llm-only       # 仅重新配置 LLM
xivbot config --deepxiv-only   # 仅重新配置 DeepXiv Token
xivbot config --bots-only      # 仅重新配置 Bot 平台
```

### 3. 终端对话

```bash
xivbot chat                  # 多轮交互式终端对话
xivbot chat --verbose        # 显示 Agent 推理步骤和工具调用
xivbot ask "2024年最好的 RAG 论文有哪些？"
```

### 4. 启动 Bot 服务

```bash
xivbot start                 # 启动所有已配置的 Bot
xivbot start --verbose       # 显示每条消息和工具调用的实时日志
xivbot start --telegram      # 仅启动 Telegram
xivbot start --feishu        # 仅启动飞书
```

### 5. 查看状态

```bash
xivbot status                # 显示配置、会话数量、记忆统计
```

---

## Bot 命令

以下命令在 **Telegram 和飞书** 上完全一致：

| 命令 | 说明 |
|------|------|
| `/start` | 欢迎消息 |
| `/help` | 完整命令帮助 |
| `/status` | 当前配置、会话信息、记忆统计 |
| `/sessions` | 列出所有会话（最新在前） |
| `/newsession` | 新建会话 |
| `/switch <n>` | 切换到第 `n` 个会话 |
| `/deletesession` | 交互式删除：显示列表，按序号或 `all` 删除 |
| `/deletesession 1 3` | 直接删除会话 1 和 3 |
| `/deletesession all` | 删除所有会话 |
| `/reset` | 清空当前会话的对话历史 |
| `/note` | 对当前 session 最近一篇论文生成 AI 笔记 |
| `/note <arxiv_id>` | 对指定论文生成笔记 |
| `/digest` | 生成**今天**的阅读摘要（返回 `.md` 文件） |
| `/digest this_week` | 最近 7 天的摘要 |
| `/digest last_week` | 上周的摘要 |
| `/digest last_month` | 上个月的摘要 |
| `/digest 2026-02-25` | 指定日期的摘要 |
| `/cancel` | 取消任何待确认的两步操作 |

发送 `hi`、`hello`、`你好` 等问候语也会显示状态面板。

---

## 后台任务

对于耗时较长的 Agent 任务（如调研数十篇论文并批量写笔记），可使用 `/backrun` 将任务提交到后台运行，期间可继续正常聊天。

```
用户：/backrun 帮我调研 50 篇今年的 agentic memory paper，全部写好笔记，最后给我 md

Bot： Background task started  [id: a1b2c3]
      Use /bgtasks to check status.
      Use /bgresult a1b2c3 to get the result when done.
```

随时查看进度：

```
用户：/bgtasks

Bot： Background Tasks

      1. [a1b2c3] 🔄 running     帮我调研 50 篇今年的 agentic memory...
           started: 2026-02-25 14:30  → /bgcancel a1b2c3 to cancel
```

完成后获取结果 `.md` 文件：

```
用户：/bgresult 1

Bot： 📎 20260225_143000_a1b2c3_result.md
```

| 命令 | 说明 |
|------|------|
| `/backrun <任务描述>` | 在后台启动一个长时 Agent 任务 |
| `/bgtasks` | 列出所有后台任务及状态（pending / running / done / failed） |
| `/bgresult <n>` | 获取第 `n` 个任务（或按短 ID）的结果，以 `.md` 文件发送 |
| `/bgcancel <n>` | 在下一个工具调用边界处取消正在运行的任务 |

每个后台任务使用**独立的 Agent 实例**（与当前会话完全隔离），最多支持 **40 轮工具调用**，适合批量研究工作流。

---

## Agent 技能

LLM 在对话中可自动调用以下工具：

| 技能 | 说明 |
|------|------|
| `search_papers` | 语义 + 关键词混合搜索 arXiv 论文 |
| `get_paper_metadata` | 完整元数据：标题、作者、摘要、分类、章节目录 |
| `get_paper_brief` | 快速获取 TLDR + 关键词 + 引用数 |
| `get_paper_preview` | 论文全文前 ~10 000 字 |
| `read_paper_section` | 指定章节内容（Introduction、Methods、Conclusion 等） |
| `get_full_paper` | 完整论文 Markdown 全文 |
| `get_pmc_metadata` | PubMed Central 论文元数据 |
| `get_pmc_full` | PMC 论文完整内容 |
| `batch_paper_briefs` | 批量获取多篇论文的快速摘要 |
| `recall_papers` | 按主题和/或日期搜索个人阅读历史 |
| `read_paper_notes` | 读取指定论文的已保存笔记 |
| `list_noted_papers` | 列出所有有笔记的论文 |

---

## 功能说明

### 阅读记忆与召回

XivBot 在后台自动将你深度阅读过的论文（调用过 `get_paper_brief`、`get_paper_preview`、`read_paper_section` 等工具的论文）记录到 `workspace/memory/`。每张记忆卡片保存论文标题、TLDR、关键词、arXiv 分类和访问时间。

当你问：

- *"我最近看了啥 paper"*
- *"那几篇关于具身智能的论文"*
- *"我这周看的 RAG 相关论文"*

Agent 会优先调用 `recall_papers`。若日期或关键词过滤没有结果，会自动扩展搜索范围确保给出答案。

### 笔记功能

```
用户：/note
Bot： [2506.23351] RoboTwin 现有笔记：（如有则展示）
      请发送笔记指令：
      例如：'总结创新点，200字' / '记录实验结果' / 'summarise contributions'
      （发送 /cancel 取消）

用户：总结这篇论文的实验结果，200字以内

Bot： ✅ 笔记已保存 (id: a1b2c3d4)
      ── 生成的笔记 ──────────────
      本文实验在 RoboTwin 基准上评估了…
```

Bot 会自动获取论文摘要和正文，结合你的指令，用 LLM 生成笔记后保存。

笔记存储在 `workspace/notes/<arxiv_id>.json`。

### 阅读摘要

```
用户：/digest today
Bot： 正在生成今天的阅读摘要…
      📎 digest_today_2026-02-25.md（发送文件）
```

摘要文件结构：
1. `# Reading Digest — today` + 整体概述
2. 每篇论文一个 `## [arxiv_id] 标题` 章节，包含论文总结和已保存笔记
3. `## Key Takeaways` — 跨论文主题综合

摘要同时保存到 `workspace/digests/`。

---

## 工作区目录结构

```
~/.xivbot/config.json              ← 配置文件（权限 0600）

<workspace>/                       ← 默认：~/xivbot_workspace/
├── sessions/
│   └── <chat_id>/
│       ├── <session_id>.json      ← 每个会话的对话历史
│       └── _state.json            ← 当前活跃会话指针
├── memory/
│   ├── index.json                 ← 按日期 / 会话 / 聊天的索引
│   └── papers/
│       └── <arxiv_id>.json        ← 每篇论文的记忆卡片
├── notes/
│   └── <arxiv_id>.json            ← 每篇论文的用户笔记
├── digests/
│   └── digest_today_2026-02-25.md ← 生成的阅读摘要
└── bg_tasks/
    └── <chat_id>/
        ├── <task_id>.json         ← 任务元数据（状态、时间戳、prompt）
        └── <task_id>_result.md    ← 任务完成后的结果文件
```

---

## 平台配置

### Telegram

1. 向 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，创建一个新 Bot
2. 复制 Bot Token
3. 运行 `xivbot config --bots-only`，填入 Token

XivBot 使用**长轮询**方式，无需公网 IP 或服务器配置。

### 飞书 / Lark

1. 访问 [open.feishu.cn](https://open.feishu.cn)，创建企业自建应用
2. 在"添加应用能力"中开启 **机器人** 能力
3. 订阅事件 `im.message.receive_v1`
4. 配置事件回调 URL：`http://<你的服务器>:<端口>/feishu/event`
5. 运行 `xivbot config --bots-only`，填入 App ID、App Secret、Verification Token

默认监听端口为 8080。若服务器在内网，可使用 [ngrok](https://ngrok.com) 或反向代理对外暴露。

---

## 支持的 LLM 提供商

| 标识 | 提供商 | 示例模型 |
|------|--------|---------|
| `openai` | OpenAI | gpt-4o, gpt-4o-mini |
| `claude` | Anthropic | claude-3-5-sonnet-20241022 |
| `deepseek` | DeepSeek | deepseek-chat, deepseek-reasoner |
| `xai` | xAI (Grok) | grok-2-1212 |
| `zhipu` | 智谱 AI | glm-4-plus, glm-4-air |
| `minimax` | MiniMax | abab6.5s-chat |
| `kimi` | 月之暗面 | moonshot-v1-32k |

所有提供商均使用 OpenAI 兼容接口，切换提供商只需修改 `~/.xivbot/config.json`。

---

## 配置文件

`~/.xivbot/config.json`（权限 0600）：

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
