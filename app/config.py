"""Config + secrets. malveon.yaml holds tuning, env holds credentials."""
from __future__ import annotations

import functools
import logging
import os
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Load .env for local runs. Railway injects real environment variables, so
# override=False means a deployed value always wins over a stale local file.
# Without this the keys sit in .env and are silently invisible to the app —
# every provider just looks "not configured" and degrades quietly.
try:
    from dotenv import load_dotenv          # ships with uvicorn[standard]
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

# httpx logs "HTTP Request: GET <full-url>" at INFO by default. Some providers
# (MillionVerifier) only accept auth as a URL query param, so at INFO level
# every verify call writes the live key into plaintext logs — on Railway that
# means the log viewer. Our own collectors already log structured, useful
# summaries (records/signals per run); httpx's raw per-request dump was never
# something anything here reads. WARNING+ keeps real transport errors visible
# while dropping the routine request-URL logging that leaks secrets.
logging.getLogger("httpx").setLevel(logging.WARNING)
# Railway mounts the persistent volume here; locally it falls back to ./data.
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "lead.db"


@functools.lru_cache(maxsize=1)
def cfg() -> dict:
    with open(os.getenv("CONFIG_PATH", ROOT / "malveon.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def reload_cfg() -> dict:
    """Re-read malveon.yaml without a restart — retuning weights is the main loop."""
    cfg.cache_clear()
    return cfg()


def _env(name: str, default: str = "") -> str:
    """Strip whitespace from every credential.

    A .env written on Windows has CRLF endings, and a trailing \\r survives
    into the value when it is piped anywhere. httpx then rejects the whole
    request with "Illegal header value" BEFORE any network call, so the
    provider looks unreachable rather than misconfigured — 25 GitHub lookups
    "failed" in 2.5 seconds with no request ever leaving the container.
    """
    return (os.getenv(name, default) or "").strip()


class Env:
    GITHUB_TOKEN = _env("GITHUB_TOKEN")
    MILLIONVERIFIER_KEY = _env("MILLIONVERIFIER_KEY")
    PDL_KEY = _env("PDL_KEY")
    LLM_API_KEY = _env("LLM_API_KEY")
    LLM_MODEL = os.getenv("LLM_MODEL", "accounts/fireworks/models/deepseek-v4-flash-0731")
    # Anthropic and OpenAI-compatible hosts (Fireworks, Groq, OpenRouter,
    # Together, local vLLM) speak different wire formats. Inferred from the key
    # prefix unless set explicitly — a mismatched base is a silent 401, which is
    # exactly how a misconfigured classifier looks like "no key configured".
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "") or (
        "anthropic" if os.getenv("LLM_API_KEY", "").startswith("sk-ant-") else "openai")
    LLM_BASE = os.getenv("LLM_BASE") or (
        "https://api.anthropic.com/v1/messages" if LLM_PROVIDER == "anthropic"
        else "https://api.fireworks.ai/inference/v1/chat/completions")
    REDDIT_ID = _env("REDDIT_CLIENT_ID")
    REDDIT_SECRET = _env("REDDIT_CLIENT_SECRET")
    # Optional. producthunt.com > Settings > API Dashboard > Developer Token.
    PRODUCTHUNT_TOKEN = _env("PRODUCTHUNT_TOKEN")
    # Optional. Reverse DNS works with no key at all; this only adds coverage.
    IPINFO_TOKEN = _env("IPINFO_TOKEN")
    # Set to any non-empty value to require ?key= on the web UI when hosted.
    UI_KEY = _env("UI_KEY")
