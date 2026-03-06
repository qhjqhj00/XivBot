"""
Configuration management for XivBot.
Config is stored at ~/.xivbot/config.json
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR = Path.home() / ".xivbot"
CONFIG_FILE = CONFIG_DIR / "config.json"

_cache_lock = threading.Lock()
_cached_config: Optional[Dict[str, Any]] = None
_cached_mtime: float = 0.0
_CACHE_TTL = 5.0  # seconds before re-checking disk

# Supported LLM providers with their default base URLs and suggested models
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        "key_hint": "sk-...",
        "docs_url": "https://platform.openai.com/api-keys",
    },
    "claude": {
        "name": "Anthropic (Claude)",
        "base_url": "https://api.anthropic.com/v1",
        "models": [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ],
        "key_hint": "sk-ant-...",
        "docs_url": "https://console.anthropic.com/keys",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_hint": "sk-...",
        "docs_url": "https://platform.deepseek.com/api_keys",
    },
    "xai": {
        "name": "xAI (Grok)",
        "base_url": "https://api.x.ai/v1",
        "models": ["grok-2-1212", "grok-2-vision-1212", "grok-beta"],
        "key_hint": "xai-...",
        "docs_url": "https://console.x.ai/",
    },
    "zhipu": {
        "name": "ZhipuAI (GLM)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash", "glm-4"],
        "key_hint": "...",
        "docs_url": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    "minimax": {
        "name": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "models": ["abab6.5s-chat", "abab6.5-chat", "abab5.5-chat"],
        "key_hint": "...",
        "docs_url": "https://www.minimaxi.com/",
    },
    "kimi": {
        "name": "Kimi (Moonshot AI)",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "key_hint": "sk-...",
        "docs_url": "https://platform.moonshot.cn/console/api-keys",
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o",
            "google/gemini-2.0-flash-001",
            "deepseek/deepseek-chat-v3-0324",
            "meta-llama/llama-3.3-70b-instruct",
        ],
        "key_hint": "sk-or-...",
        "docs_url": "https://openrouter.ai/keys",
    },
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "workspace": {
        "path": str(Path.home() / "xivbot_workspace"),
    },
    "llm": {
        "provider": None,
        "api_key": None,
        "model": None,
        "base_url": None,
    },
    "deepxiv": {
        "api_key": None,
    },
    "bots": {
        "feishu": {
            "enabled": False,
            "app_id": None,
            "app_secret": None,
            "verification_token": None,
            "encrypt_key": None,
            "port": 8080,
        },
        "telegram": {
            "enabled": False,
            "bot_token": None,
        },
    },
}


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Dict[str, Any]:
    """Load config from disk with in-memory caching (TTL-based invalidation)."""
    global _cached_config, _cached_mtime

    with _cache_lock:
        now = time.monotonic()
        if _cached_config is not None and (now - _cached_mtime) < _CACHE_TTL:
            return _deep_copy(_cached_config)

    ensure_config_dir()
    if not CONFIG_FILE.exists():
        result = _deep_copy(DEFAULT_CONFIG)
    else:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
            result = _deep_merge(DEFAULT_CONFIG, stored)
        except (json.JSONDecodeError, OSError):
            result = _deep_copy(DEFAULT_CONFIG)

    with _cache_lock:
        _cached_config = _deep_copy(result)
        _cached_mtime = time.monotonic()

    return result


def save_config(config: Dict[str, Any]) -> None:
    """Persist config to disk and invalidate cache."""
    global _cached_config, _cached_mtime
    ensure_config_dir()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.chmod(CONFIG_FILE, 0o600)
    with _cache_lock:
        _cached_config = _deep_copy(config)
        _cached_mtime = time.monotonic()


def get(key_path: str, default: Any = None) -> Any:
    """
    Get a config value by dot-separated key path.
    e.g. get("llm.api_key"), get("bots.telegram.bot_token")
    """
    config = load_config()
    keys = key_path.split(".")
    val = config
    for k in keys:
        if not isinstance(val, dict) or k not in val:
            return default
        val = val[k]
    return val


def set_value(key_path: str, value: Any) -> None:
    """Set a config value by dot-separated key path and persist."""
    config = load_config()
    keys = key_path.split(".")
    node = config
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    save_config(config)


def is_configured() -> bool:
    """Return True if minimum viable config (LLM + deepxiv key) is set."""
    cfg = load_config()
    return bool(
        cfg.get("llm", {}).get("api_key")
        and cfg.get("deepxiv", {}).get("api_key")
    )


def get_llm_config() -> Dict[str, Optional[str]]:
    cfg = load_config()
    return cfg.get("llm", {})


def get_deepxiv_token() -> Optional[str]:
    token = get("deepxiv.api_key")
    if token:
        return token
    # Fallback to environment variable
    return os.environ.get("DEEPXIV_TOKEN")


def get_provider_info(provider_key: str) -> Optional[Dict[str, Any]]:
    return PROVIDERS.get(provider_key)


_openai_client = None
_openai_client_key: Optional[str] = None


def get_openai_client():
    """Return a cached OpenAI client using the current LLM config."""
    global _openai_client, _openai_client_key
    llm = get_llm_config()
    cache_key = f"{llm.get('api_key')}|{llm.get('base_url')}"
    if _openai_client is not None and _openai_client_key == cache_key:
        return _openai_client
    from openai import OpenAI
    _openai_client = OpenAI(api_key=llm["api_key"], base_url=llm.get("base_url"))
    _openai_client_key = cache_key
    return _openai_client


def get_workspace_dir() -> Path:
    """Return the workspace root path, creating subdirs if needed."""
    path = Path(get("workspace.path") or str(Path.home() / "xivbot_workspace"))
    for sub in ("sessions", "context", "notes"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


# ── helpers ──────────────────────────────────────────────────────────────────

def _deep_copy(d: Dict) -> Dict:
    return json.loads(json.dumps(d))


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Recursively merge override into a copy of base."""
    result = _deep_copy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
