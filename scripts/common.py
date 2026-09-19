"""Shared utilities for the Daily Tool Factory pipeline.

Every script in this directory imports from here so that config loading,
registry access, daily-log bookkeeping, budget tracking and LLM calls behave
identically across all pipeline steps (Steps A-G).
"""
from __future__ import annotations

import datetime
import difflib
import json
import os
import pathlib
import re

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
LOGS_DIR = ROOT / "logs"
TEMPLATES_DIR = ROOT / "templates"
TOOLS_DIR = ROOT / "tools"
TEST_TMP = ROOT / ".testtmp"

STOPWORDS = set(
    """a an and are as at be been being by can could did do does doing for from
get got has have how i if in into is it its me my no not of on or our ours out
over own she he they them their the this that these those to too very was we
were what when where which who why will with you your up down just than then
so such t don should now d ll m o re ve y s using use used make made makes
tool tools calculator converters converter generator formatter checker online
free web based simple easy""".split()
)


# ---------------------------------------------------------------- basics ----

def utc_today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def load_json(path: pathlib.Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


# ---------------------------------------------------------------- config ----

_CONFIG = None
_CATEGORIES = None


def config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_json(CONFIG_DIR / "site.json", {})
    return _CONFIG


def categories() -> list:
    """Category metadata dicts from config/categories.json."""
    global _CATEGORIES
    if _CATEGORIES is None:
        data = load_json(CONFIG_DIR / "categories.json", {"categories": []})
        _CATEGORIES = data.get("categories", [])
    return _CATEGORIES


def category_map() -> dict:
    """slug -> {name, description}"""
    return {c["slug"]: c for c in categories()}


def allowed_category_slugs() -> list:
    return [c["slug"] for c in categories()]


def domain() -> str:
    return config()["domain"].rstrip("/")


# --------------------------------------------------------------- registry ---

def registry_path() -> pathlib.Path:
    return ROOT / "registry.json"


def registry() -> dict:
    data = load_json(registry_path(), {"tools": []})
    if isinstance(data, list):  # very old shape tolerance
        data = {"tools": data}
    data.setdefault("tools", [])
    return data


def save_registry(reg: dict) -> None:
    save_json(registry_path(), reg)


def registry_tools() -> list:
    return registry()["tools"]


def published_today_count() -> int:
    today = utc_today()
    return sum(1 for t in registry_tools() if t.get("date_published") == today and t.get("status") == "published")


# ------------------------------------------------------------- daily log ----

def daily_log_path(date: str | None = None) -> pathlib.Path:
    return LOGS_DIR / f"{date or utc_today()}.json"


def fresh_daily_log(date: str) -> dict:
    return {
        "date": date,
        "runs": [],
        "ideas_proposed": [],
        "ideas_rejected": [],
        "tools_published": [],
        "tools_failed": [],
        "llm_calls": [],
        "budget": {"tokens": 0, "usd": 0.0},
    }


def load_daily_log() -> dict:
    path = daily_log_path()
    log = load_json(path, None)
    if not isinstance(log, dict):
        log = fresh_daily_log(utc_today())
    for key, default in fresh_daily_log(log.get("date", utc_today())).items():
        log.setdefault(key, default)
    return log


def save_daily_log(log: dict) -> None:
    save_json(daily_log_path(log.get("date", utc_today())), log)


def log_append(section: str, item) -> None:
    log = load_daily_log()
    log[section].append(item)
    save_daily_log(log)


def log_note(message: str) -> None:
    """Append a timestamped note into today's run-notes (runs[-1])."""
    log = load_daily_log()
    if not log["runs"]:
        log["runs"].append({"started_at": utc_now_iso(), "notes": [], "status": "running"})
    log["runs"][-1].setdefault("notes", []).append(f"{utc_now_iso()} {message}")
    save_daily_log(log)


# ---------------------------------------------------------------- budget ----

class BudgetExceeded(Exception):
    pass


def _price_for(model: str) -> tuple[float, float]:
    table = config()["llm"].get("price_per_mtok", {})
    entry = table.get(model) or table.get("_default") or {"input": 1.0, "output": 3.0}
    return float(entry["input"]), float(entry["output"])


def budget_status() -> dict:
    log = load_daily_log()
    return {"tokens": log["budget"]["tokens"], "usd": log["budget"]["usd"]}


def _check_budget() -> None:
    cfg = config()["llm"]
    b = budget_status()
    if b["tokens"] >= cfg["daily_token_budget"]:
        raise BudgetExceeded(
            f"token budget reached: {b['tokens']}/{cfg['daily_token_budget']}"
        )
    if b["usd"] >= cfg["daily_cost_budget_usd"]:
        raise BudgetExceeded(
            f"cost budget reached: ${b['usd']:.2f}/${cfg['daily_cost_budget_usd']:.2f}"
        )


# -------------------------------------------------------------------- LLM ---

MODEL_FALLBACKS = {
    "idea": [
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "gemma2-9b-it",
        "qwen/qwen3-32b",
    ],
    "code": [
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "moonshotai/kimi-k2-instruct-0905",
        "qwen/qwen3-32b",
        "llama-3.1-8b-instant",
    ],
}

_MODEL_CACHE = None


def list_models() -> list:
    """Model ids available to this account (cached per process)."""
    global _MODEL_CACHE
    if _MODEL_CACHE is None:
        api_key = os.environ.get("LLM_API_KEY", "")
        if not api_key:
            raise RuntimeError("LLM_API_KEY environment variable is not set")
        api_base = config()["llm"].get("api_base", "https://api.groq.com/openai/v1").rstrip("/")
        resp = requests.get(f"{api_base}/models",
                            headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
        if resp.status_code == 403:
            raise RuntimeError(
                "LLM API returned 403 Forbidden on /models - key revoked, "
                "region-blocked or account restricted."
            )
        resp.raise_for_status()
        _MODEL_CACHE = [m.get("id", "") for m in resp.json().get("data", [])]
    return _MODEL_CACHE


def resolve_model(kind: str) -> str:
    """Resolve the configured model for 'idea'/'code' to one this account can
    actually use, walking a fallback chain when models are deprecated."""
    cfg = config()["llm"]
    preferred = cfg["idea_model"] if kind == "idea" else cfg["code_model"]
    available = list_models()
    if preferred in available:
        return preferred
    for cand in MODEL_FALLBACKS.get(kind, []):
        if cand in available:
            return cand
    for m in available:
        if any(k in m for k in ("llama", "gpt-oss", "qwen", "kimi", "gemma")):
            return m
    raise RuntimeError(f"no usable chat model available; account models: {available[:20]}")


def groq_chat(model: str, messages: list, max_tokens: int = 2000,
              temperature: float = 0.5, json_mode: bool = False,
              purpose: str = "") -> str:
    """One chat completion against the configured provider (Groq, OpenAI-compatible).

    Tracks usage into the daily log budget; raises BudgetExceeded before
    making a call once either cap is hit.
    """
    _check_budget()
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_API_KEY environment variable is not set")
    api_base = config()["llm"].get("api_base", "https://api.groq.com/openai/v1").rstrip("/")

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    last_error = None
    for attempt in range(3):  # retries with backoff (rate limits are often per-minute)
        try:
            resp = requests.post(
                f"{api_base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body,
                timeout=180,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if attempt < 2:
                    import time
                    time.sleep(35 if resp.status_code == 429 else 10)
                    continue
                raise requests.RequestException(last_error)
            if resp.status_code == 403:
                raise RuntimeError(
                    "LLM API returned 403 Forbidden - the API key is likely "
                    "revoked, region-blocked, or the account is restricted. "
                    "Check the key in repo secrets (LLM_API_KEY)."
                )
            if resp.status_code == 401:
                raise RuntimeError(
                    "LLM API returned 401 Unauthorized - the API key is invalid."
                )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {}) or {}
            _record_llm_usage(purpose, model, usage)
            return content
        except RuntimeError:
            raise
        except Exception as e:  # transient -> retry once
            last_error = str(e)
            if attempt == 0:
                import time
                time.sleep(6)
    raise RuntimeError(f"LLM call failed after retry: {last_error}")


def _record_llm_usage(purpose: str, model: str, usage: dict) -> None:
    prompt_toks = int(usage.get("prompt_tokens", 0) or 0)
    compl_toks = int(usage.get("completion_tokens", 0) or 0)
    total = int(usage.get("total_tokens", prompt_toks + compl_toks) or 0)
    in_price, out_price = _price_for(model)
    est_usd = (prompt_toks * in_price + compl_toks * out_price) / 1_000_000.0

    log = load_daily_log()
    log["llm_calls"].append({
        "time": utc_now_iso(),
        "purpose": purpose,
        "model": model,
        "prompt_tokens": prompt_toks,
        "completion_tokens": compl_toks,
        "total_tokens": total,
        "est_cost_usd": round(est_usd, 6),
    })
    log["budget"]["tokens"] += total
    log["budget"]["usd"] = round(log["budget"]["usd"] + est_usd, 6)
    save_daily_log(log)


# ------------------------------------------------------------- text utils ---

def normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def content_tokens(s: str) -> set:
    return {t for t in normalize_text(s).split() if len(t) > 1 and t not in STOPWORDS}


def token_overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:64] or "tool"


def parse_llm_json(text: str):
    """Parse JSON out of an LLM response, tolerating fences and prose."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            pass
    i, j = t.find("["), t.rfind("]")
    if i != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            pass
    raise ValueError(f"no valid JSON in LLM output: {t[:200]!r}")


def truncate_chars(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    cut = s[: limit - 3].rsplit(" ", 1)[0]
    return (cut or s[: limit - 3]).rstrip(",;: ") + "..."


def html_escape(s: str) -> str:
    import html as _html
    return _html.escape(s or "", quote=True)


def ensure_dirs() -> None:
    for d in (LOGS_DIR, TOOLS_DIR, TEST_TMP):
        d.mkdir(parents=True, exist_ok=True)
