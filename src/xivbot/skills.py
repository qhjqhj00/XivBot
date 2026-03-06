"""
XivBot Default Skills — DeepXiv SDK capabilities exposed as callable tools.

Each skill is a Python function decorated with @skill.  The agent_runner
collects all registered skills and converts them into OpenAI function-call
tool definitions so the LLM can invoke them automatically.
"""
from __future__ import annotations

import json
import traceback
from typing import Any, Callable, Dict, List, Optional


# ── Skill registry ────────────────────────────────────────────────────────────

_SKILLS: List[Dict[str, Any]] = []


def skill(
    description: str,
    parameters: Dict[str, Any],
):
    """
    Decorator that registers a function as an agent skill.

    Args:
        description: Human-readable description shown to the LLM.
        parameters:  JSON-Schema style parameters dict.
    """

    def decorator(fn: Callable) -> Callable:
        _SKILLS.append(
            {
                "name": fn.__name__,
                "description": description,
                "parameters": parameters,
                "fn": fn,
            }
        )
        return fn

    return decorator


def get_all_skills() -> List[Dict[str, Any]]:
    """Return the full list of registered skills."""
    return _SKILLS


def get_openai_tools() -> List[Dict[str, Any]]:
    """Convert registered skills to OpenAI function-call tool definitions."""
    tools = []
    for s in _SKILLS:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": s["name"],
                    "description": s["description"],
                    "parameters": s["parameters"],
                },
            }
        )
    return tools


def call_skill(
    name: str,
    arguments: Dict[str, Any],
    reader,
    session_id: str = "",
    chat_id: str = "",
) -> str:
    """
    Execute a skill by name.

    Args:
        name:       Skill function name.
        arguments:  Dict of keyword arguments from the LLM.
        reader:     deepxiv_sdk.Reader instance.
        session_id: Current session ID (for memory recording).
        chat_id:    Current chat ID (for memory recording).

    Returns:
        Result as a string (truncated if very long).
    """
    from .memory_store import DEEP_SKILLS, get_memory_store
    import threading

    for s in _SKILLS:
        if s["name"] == name:
            try:
                result = s["fn"](reader=reader, **arguments)
                text = _to_str(result)

                # Fire-and-forget memory write for deep skills
                if name in DEEP_SKILLS and session_id:
                    arxiv_id = (
                        arguments.get("arxiv_id")
                        or arguments.get("pmc_id")
                    )
                    if arxiv_id:
                        threading.Thread(
                            target=get_memory_store().record_access,
                            args=(arxiv_id, session_id, chat_id, name, reader),
                            daemon=True,
                        ).start()

                # Hard cap to avoid blowing the context window
                if len(text) > 12_000:
                    text = text[:12_000] + "\n\n[…output truncated…]"
                return text
            except Exception:
                return f"[Skill error]\n{traceback.format_exc()}"
    return f"[Unknown skill: {name}]"


def _to_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


# ── Skill definitions ─────────────────────────────────────────────────────────

@skill(
    description=(
        "Search arXiv papers by keyword / semantic query. "
        "Returns a ranked list with titles, arXiv IDs, abstracts, and citation counts. "
        "Use this first when the user asks about a research topic or wants to find papers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query, e.g. 'agent memory mechanisms'",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of results to return (default 10, max 20)",
                "default": 10,
            },
            "mode": {
                "type": "string",
                "enum": ["hybrid", "bm25", "vector"],
                "description": "Search mode: hybrid (default), bm25, or vector",
                "default": "hybrid",
            },
            "categories": {
                "type": "string",
                "description": "Comma-separated arXiv category filter, e.g. 'cs.AI,cs.CL'",
            },
            "min_citations": {
                "type": "integer",
                "description": "Minimum number of citations",
            },
            "date_from": {
                "type": "string",
                "description": "Earliest publication date, format YYYY-MM-DD",
            },
            "date_to": {
                "type": "string",
                "description": "Latest publication date, format YYYY-MM-DD",
            },
        },
        "required": ["query"],
    },
)
def search_papers(
    query: str,
    limit: int = 10,
    mode: str = "hybrid",
    categories: Optional[str] = None,
    min_citations: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    reader=None,
) -> str:
    cat_list = [c.strip() for c in categories.split(",")] if categories else None
    results = reader.search(
        query=query,
        size=min(limit, 20),
        search_mode=mode,
        categories=cat_list,
        min_citation=min_citations,
        date_from=date_from,
        date_to=date_to,
    )
    if not results:
        return "No results found or API error."

    total = results.get("total", 0)
    items = results.get("results", [])
    lines = [f"Found {total} papers (showing {len(items)}):\n"]
    for i, p in enumerate(items, 1):
        lines.append(
            f"{i}. [{p.get('arxiv_id')}] {p.get('title', 'No title')}\n"
            f"   Citations: {p.get('citation', 0)} | Score: {p.get('score', 0):.3f}\n"
            f"   {p.get('abstract', '')[:200]}…\n"
        )
    return "\n".join(lines)


@skill(
    description=(
        "Get detailed metadata of a specific arXiv paper: title, authors, "
        "abstract, categories, publication date, citation count, and section outline."
    ),
    parameters={
        "type": "object",
        "properties": {
            "arxiv_id": {
                "type": "string",
                "description": "arXiv paper ID, e.g. '2409.05591'",
            }
        },
        "required": ["arxiv_id"],
    },
)
def get_paper_metadata(arxiv_id: str, reader=None) -> str:
    result = reader.head(arxiv_id)
    if not result:
        return f"Could not retrieve metadata for {arxiv_id}."
    return json.dumps(result, ensure_ascii=False, indent=2)


@skill(
    description=(
        "Get a brief summary of an arXiv paper: TLDR, keywords, and citation count. "
        "Much cheaper than reading the full paper – use this for quick triage "
        "(deciding whether the paper is worth deeper reading), not as the only "
        "source when introducing a paper in detail."
    ),
    parameters={
        "type": "object",
        "properties": {
            "arxiv_id": {
                "type": "string",
                "description": "arXiv paper ID, e.g. '2409.05591'",
            }
        },
        "required": ["arxiv_id"],
    },
)
def get_paper_brief(arxiv_id: str, reader=None) -> str:
    result = reader.brief(arxiv_id)
    if not result:
        return f"Could not retrieve brief for {arxiv_id}."
    lines = [
        f"Title: {result.get('title', 'N/A')}",
        f"arXiv ID: {result.get('arxiv_id', arxiv_id)}",
        f"Published: {result.get('publish_at', 'N/A')}",
        f"Citations: {result.get('citations', 0)}",
    ]
    if result.get("keywords"):
        kws = result["keywords"]
        if isinstance(kws, list):
            lines.append(f"Keywords: {', '.join(kws)}")
        else:
            lines.append(f"Keywords: {kws}")
    if result.get("tldr"):
        lines.append(f"\nTLDR:\n{result['tldr']}")
    return "\n".join(lines)


@skill(
    description=(
        "Get a preview (~10 000 characters) of an arXiv paper's full text. "
        "Good for understanding the overall content without loading everything."
    ),
    parameters={
        "type": "object",
        "properties": {
            "arxiv_id": {
                "type": "string",
                "description": "arXiv paper ID",
            }
        },
        "required": ["arxiv_id"],
    },
)
def get_paper_preview(arxiv_id: str, reader=None) -> str:
    result = reader.preview(arxiv_id)
    if not result:
        return f"Could not retrieve preview for {arxiv_id}."
    content = result.get("content") or result.get("preview") or str(result)
    return content


@skill(
    description=(
        "Read a specific named section of an arXiv paper (e.g. 'Introduction', "
        "'Methods', 'Related Work', 'Conclusion'). "
        "Prefer this over get_full_paper when you only need one section."
    ),
    parameters={
        "type": "object",
        "properties": {
            "arxiv_id": {
                "type": "string",
                "description": "arXiv paper ID",
            },
            "section_name": {
                "type": "string",
                "description": "Section name (case-insensitive), e.g. 'Introduction'",
            },
        },
        "required": ["arxiv_id", "section_name"],
    },
)
def read_paper_section(arxiv_id: str, section_name: str, reader=None) -> str:
    content = reader.section(arxiv_id, section_name)
    if not content:
        return f"Section '{section_name}' not found in {arxiv_id}."
    return content


@skill(
    description=(
        "Get the full markdown text of an arXiv paper. "
        "Use only when you really need the complete paper; prefer get_paper_preview or "
        "read_paper_section for targeted reading."
    ),
    parameters={
        "type": "object",
        "properties": {
            "arxiv_id": {
                "type": "string",
                "description": "arXiv paper ID",
            }
        },
        "required": ["arxiv_id"],
    },
)
def get_full_paper(arxiv_id: str, reader=None) -> str:
    content = reader.raw(arxiv_id)
    if not content:
        return f"Could not retrieve full text for {arxiv_id}."
    return content


@skill(
    description=(
        "Get metadata for a PubMed Central (PMC) paper. "
        "Use when the user provides a PMC ID like 'PMC544940'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pmc_id": {
                "type": "string",
                "description": "PMC paper ID, e.g. 'PMC544940'",
            }
        },
        "required": ["pmc_id"],
    },
)
def get_pmc_metadata(pmc_id: str, reader=None) -> str:
    result = reader.pmc_head(pmc_id)
    if not result:
        return f"Could not retrieve PMC metadata for {pmc_id}."
    return json.dumps(result, ensure_ascii=False, indent=2)


@skill(
    description=(
        "Get the full structured content of a PubMed Central (PMC) paper as JSON."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pmc_id": {
                "type": "string",
                "description": "PMC paper ID, e.g. 'PMC544940'",
            }
        },
        "required": ["pmc_id"],
    },
)
def get_pmc_full(pmc_id: str, reader=None) -> str:
    result = reader.pmc_json(pmc_id)
    if not result:
        return f"Could not retrieve PMC content for {pmc_id}."
    return json.dumps(result, ensure_ascii=False, indent=2)


@skill(
    description=(
        "Get a quick brief (title + TLDR + citations) for multiple arXiv papers at once. "
        "Useful for comparing a batch of papers without loading them individually."
    ),
    parameters={
        "type": "object",
        "properties": {
            "arxiv_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of arXiv IDs to fetch, e.g. ['2409.05591', '2301.07543']",
            }
        },
        "required": ["arxiv_ids"],
    },
)
def batch_paper_briefs(arxiv_ids: List[str], reader=None) -> str:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ids = arxiv_ids[:10]

    def _fetch(aid: str) -> str:
        try:
            brief = reader.brief(aid)
            if brief:
                return (
                    f"[{aid}] {brief.get('title', 'N/A')}\n"
                    f"  Citations: {brief.get('citations', 0)}\n"
                    f"  TLDR: {brief.get('tldr', 'N/A')}"
                )
        except Exception:
            pass
        return f"[{aid}] Not found."

    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(len(ids), 5)) as pool:
        futures = {pool.submit(_fetch, aid): aid for aid in ids}
        for f in as_completed(futures):
            results[futures[f]] = f.result()

    parts = [results[aid] for aid in ids]
    return "\n\n".join(parts) if parts else "No results."


@skill(
    description=(
        "Search the user's PERSONAL reading history — papers they have previously read or browsed. "
        "MUST be called first whenever the user asks about papers they viewed/read/looked at before, e.g.: "
        "'我最近看过什么论文', '我看了哪些paper', 'papers I read yesterday about RAG', "
        "'what did I look at last week', '我之前看的那篇XXX'. "
        "Pass query='' to list all recently memorised papers. "
        "Returns matching paper briefs with arXiv URLs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords or topic to search in past papers, e.g. 'lung cancer drug'",
            },
            "date_hint": {
                "type": "string",
                "description": (
                    "Optional date filter. Accepts: 'today', 'yesterday', "
                    "'this_week' (rolling last 7 days, use for '最近/recently'), "
                    "'last_week' (the calendar week before this one), "
                    "'last_month', a full date '2026-02-24', or a month '2026-02'. "
                    "Omit entirely to search across all time."
                ),
            },
        },
        "required": ["query"],
    },
)
def recall_papers(
    query: str,
    date_hint: Optional[str] = None,
    reader=None,
) -> str:
    from .memory_store import get_memory_store, resolve_date_range

    store = get_memory_store()
    date_from, date_to = resolve_date_range(date_hint) if date_hint else (None, None)

    results = store.recall(query=query, date_from=date_from, date_to=date_to)

    # If date filter produced nothing, expand to all time automatically
    used_date_hint = date_hint
    if not results and date_hint:
        results = store.recall(query=query)
        used_date_hint = None  # date filter was dropped

    # If keyword query still produced nothing, show all papers so LLM can reason
    fuzzy_fallback = False
    if not results and query:
        results = store.recall(query="")
        fuzzy_fallback = True

    if not results:
        return "No memorised papers yet."

    if fuzzy_fallback:
        header = (
            f"No exact keyword match for '{query}'. "
            f"Here are all {len(results)} memorised paper(s) — decide if any are relevant"
        )
    else:
        header = f"Found {len(results)} memorised paper(s) matching '{query}'"
        if used_date_hint:
            header += f" (date filter: {used_date_hint})"
        else:
            header += " (searched all time)"
    lines = [header + ":\n"]
    from .note_store import get_note_store
    note_store = get_note_store()
    for m in results:
        lines.append(m.brief_text())
        # Append note count hint if user has written notes on this paper
        pnf = note_store.get_notes(m.arxiv_id)
        if pnf.notes:
            lines.append(f"  Notes: {len(pnf.notes)} note(s) saved")
        lines.append("")
    return "\n".join(lines)


@skill(
    description=(
        "Read the user's personal notes (annotations/comments) for a specific arXiv paper. "
        "Use when the user asks 'what did I write about X', 'show my notes on Y', "
        "'我对这篇论文写了什么笔记', etc."
    ),
    parameters={
        "type": "object",
        "properties": {
            "arxiv_id": {
                "type": "string",
                "description": "arXiv paper ID, e.g. '2409.05591'",
            }
        },
        "required": ["arxiv_id"],
    },
)
def read_paper_notes(arxiv_id: str, reader=None) -> str:
    from .note_store import get_note_store
    pnf = get_note_store().get_notes(arxiv_id)
    if not pnf.notes:
        return f"No notes saved for {arxiv_id}."
    title = pnf.title or arxiv_id
    lines = [f"Notes for [{arxiv_id}] {title}:\n"]
    for i, n in enumerate(pnf.notes, 1):
        lines.append(f"{i}. [{n.created_at[:16].replace('T', ' ')}]")
        lines.append(f"   {n.content}")
        lines.append("")
    return "\n".join(lines)


@skill(
    description=(
        "List all arXiv papers the user has saved personal notes on, with note counts. "
        "Use when the user asks 'which papers did I annotate', '我记过笔记的论文', etc."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def list_noted_papers(reader=None) -> str:
    from .note_store import get_note_store
    papers = get_note_store().list_noted_papers()
    if not papers:
        return "No paper notes saved yet."
    lines = [f"Papers with notes ({len(papers)} total):\n"]
    for p in papers:
        lines.append(
            f"[{p['arxiv_id']}] {p['title'] or '(no title)'}\n"
            f"  {p['note_count']} note(s) · last: {p['last_note'][:16].replace('T', ' ')}"
        )
    return "\n".join(lines)


# ── System prompt ─────────────────────────────────────────────────────────────

def get_system_prompt() -> str:
    """Return the system prompt with today's date injected."""
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    return f"""
You are XivBot, an intelligent assistant for academic paper research.
Today's date is {today}.

## Tools

- recall_papers      – search the user's PERSONAL reading history (papers they viewed before)
- read_paper_notes   – read the user's personal notes/annotations for a specific paper
- list_noted_papers  – list all papers the user has saved notes on
- search_papers      – search the global arXiv database for new papers
- get_paper_metadata – get paper metadata by arXiv ID
- get_paper_brief    – get TLDR + keywords (fast, low-cost)
- get_paper_preview  – get first ~10 k chars of paper text
- read_paper_section – read a named section (Introduction, Methods, …)
- get_full_paper     – get full paper markdown (use sparingly)
- get_pmc_metadata   – PMC paper metadata
- get_pmc_full       – full PMC paper content
- batch_paper_briefs – quick briefs for multiple papers at once

## CRITICAL RULE — recall_papers vs search_papers

**ALWAYS call recall_papers FIRST** whenever the user's question contains any of:
- "我看过 / 我看了 / 我看的 / 我最近看 / 我之前看 / 我刚才看"
- "最近看过 / 最近看了 / 最近有没有 / 最近看什么"
- "I read / I looked at / I viewed / papers I've seen / what did I read"
- Any reference to papers THEY personally read or browsed

Do NOT answer these questions from memory or go straight to search_papers.
You MUST call recall_papers and use its result to answer.

## recall_papers date hints (today = {today})

- "最近/近几天/recently" → date_hint='this_week'  ← USE THIS, not 'last_week'
- "今天/today"           → date_hint='today'
- "昨天/yesterday"       → date_hint='yesterday'
- "上周/last week"       → date_hint='last_week'
- "上个月/last month"    → date_hint='last_month'
- No time hint / all-time → omit date_hint

When the user asks "what did I read" with no topic, pass query="".
When there's a topic, pass it as query even if it's not an exact keyword match —
the skill will show all stored papers as fallback so you can judge relevance.

## General strategy for new paper discovery

1. Use search_papers to find relevant papers.
2. Use get_paper_brief for quick triage only (is this paper worth deeper reading?).
3. Before introducing any paper in detail, call get_paper_metadata at least once.
4. In paper introductions, include BOTH:
   - author names (at least 1-3 representative authors), and
   - affiliation/institution info when available.
5. Never output institution/team-only intros without naming authors.
6. If authors or affiliation are missing in metadata, explicitly say unavailable; never fabricate.
7. When relevant, present institution + team style descriptions (e.g., "来自北京大学的 Di He 团队 ..."), but keep author names explicit.
8. Use read_paper_section or get_paper_preview before get_full_paper.
9. Always include the full arXiv URL: https://arxiv.org/abs/<arxiv_id>.
10. Be concise but complete.
""".strip()
