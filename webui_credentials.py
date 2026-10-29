"""Per-provider default `--model` overrides for the Web UI's Settings panel.

Lives at AUTO_WORKSPACE_BASE_DIR/credentials.json -- the same "the app
owns this" location Phase 3 established for registry.json. The file name
dates from API-key mode (removed after v0.4.11), which also stored
provider API keys and custom OpenAI/Anthropic-compatible endpoints here;
only the "cli_models" key is read or written now, so this module never
stores a secret. The name is kept so users' saved model overrides carry
over without a migration. Legacy key entries an older install left in the
file are never used, but the read-modify-write below still parses and
rewrites them unchanged, so a user who wants them gone must delete them
by hand.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from handoff_bridge import PROVIDERS, atomic_write_text

import webui_common

_CREDENTIALS_LOCK = threading.Lock()


def credentials_path() -> Path:
    # A function, not a module-level constant -- same reasoning as
    # registry_path(): tests patch AUTO_WORKSPACE_BASE_DIR to a tempdir, and
    # a constant computed at import time wouldn't see that patch. Module-
    # qualified (webui_common.AUTO_WORKSPACE_BASE_DIR), not `from
    # webui_common import AUTO_WORKSPACE_BASE_DIR`, for the same reason --
    # a `from` import would bind its own copy at *this module's* import
    # time, immune to a test patching webui_common's attribute afterward.
    return webui_common.AUTO_WORKSPACE_BASE_DIR / "credentials.json"


def _read_all_credentials_data() -> dict:
    """The raw parsed file. Never raises -- a missing, corrupt, or
    unreadable (permissions) file just means nothing is configured yet,
    same posture as read_registry()."""
    return webui_common.read_json_or_default(credentials_path(), {})


def _write_all_credentials_data(data: dict) -> None:
    path = credentials_path()
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort on platforms/filesystems that don't support chmod


def read_cli_provider_models() -> dict:
    """provider -> model string, for CLI-dispatched providers (PROVIDERS)
    that have a saved default --model override. No key is ever involved
    here, since the CLI itself handles auth. Never raises, same posture as
    _read_all_credentials_data()."""
    data = _read_all_credentials_data()
    raw = data.get("cli_models")
    if not isinstance(raw, dict):
        return {}
    result = {}
    for provider, model in raw.items():
        if provider in PROVIDERS and isinstance(model, str) and model.strip():
            result[provider] = model.strip()
    return result


# Anthropic-documented model aliases only (claude --help, confirmed against
# a real installed CLI, 2026-09-03): "an alias for the latest model (e.g.
# 'fable', 'opus', or 'sonnet')". Deliberately doesn't include e.g. "haiku"
# -- it isn't named as an example in that help text and wasn't independently
# confirmed against a real call, and guessing model identifiers is exactly
# the class of bug this list exists to prevent (see docs/release-notes.md's
# v0.4.9 entry). A user who wants a model not listed here can still type any
# full model name -- this is a <datalist>, not a closed <select>.
_CLAUDE_KNOWN_MODELS = [
    {"id": "sonnet", "label": "Sonnet (latest)"},
    {"id": "opus", "label": "Opus (latest, most capable)"},
    {"id": "fable", "label": "Fable (latest)"},
]

# Fallback copy of `agy models` (agy 1.4.8, 2026-10-28), used only when the
# live lookup (_agy_live_models) has nothing yet or fails. These were the ids
# the CLI accepted for --model then -- including non-Google models, since
# Antigravity runs Claude and open-weight models as well as Gemini. The CLI's
# own list is authoritative and changes over time, so this copy is never
# treated as proof that a saved model is wrong; it is still a <datalist>, not
# a closed <select>, so a stale entry here degrades to "one suggestion
# doesn't autofill" rather than "the only options are wrong."
_AGY_KNOWN_MODELS = [
    {"id": "gemini-4-argon-medium", "label": "Gemini 4 Argon (Medium)"},
    {"id": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)"},
    {"id": "gemini-4-argon-low", "label": "Gemini 4 Argon (Low)"},
    {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"},
    {"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)"},
    {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)"},
    {"id": "claude-opus-5-5-low", "label": "Claude Opus 5.5 (Low)"},
    {"id": "claude-opus-5-5-medium", "label": "Claude Opus 5.5 (Medium)"},
    {"id": "claude-opus-5-5-high", "label": "Claude Opus 5.5 (High)"},
    {"id": "claude-sonnet-5-5-low", "label": "Claude Sonnet 5.5 (Low)"},
    {"id": "claude-sonnet-5-5-medium", "label": "Claude Sonnet 5.5 (Medium)"},
    {"id": "claude-sonnet-5-5-high", "label": "Claude Sonnet 5.5 (High)"},
    {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)"},
]


def _codex_known_models() -> list[dict]:
    """Codex has no documented static alias list like Claude's -- its own
    CLI caches the real, current model catalog client-side at
    ~/.codex/models_cache.json (confirmed real shape, 2026-09-03:
    {"models": [{"slug", "display_name", "visibility", ...}]}), refreshed by
    the codex CLI itself. Reading it live keeps this list current without
    this project having to track codex's model releases by hand, and
    degrades to "no suggestions" (never a wrong guess) if it's missing,
    unreadable, or shaped differently than expected -- this file isn't a
    documented/stable API, just the best real source found."""
    cache_path = Path.home() / ".codex" / "models_cache.json"
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    result = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        # "hide" entries (e.g. "gpt-reserve", "codex-auto-review" as of this
        # writing) are marked not meant to be user-facing by codex itself.
        if entry.get("visibility") == "hide":
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        label = entry.get("display_name")
        result.append({"id": slug, "label": label if isinstance(label, str) and label else slug})
    return result


# `agy models` prints "Fetching available models..." and then one
# "<id><TAB><label>" line per model (agy 1.4.8). It asks Google's servers, so it
# is cached: the Settings panel polls /api/providers, and a slow or failed call
# must not stall every poll.
_AGY_MODELS_TTL_SECONDS = 600
_AGY_MODELS_FAILURE_BACKOFF_SECONDS = 60
_AGY_MODELS_TIMEOUT_SECONDS = 8
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_AGY_MODELS_LOCK = threading.Lock()
_agy_models_cache: dict = {"models": [], "ok_at": None, "failed_at": None, "refreshing": False}


def _parse_agy_models(output: str) -> list[dict]:
    models = []
    seen = set()
    for line in output.splitlines():
        model_id, sep, label = line.partition("\t")
        model_id = model_id.strip()
        if not sep or not _MODEL_ID_RE.match(model_id) or model_id in seen:
            continue  # "Fetching available models...", blank lines, anything unexpected
        seen.add(model_id)
        models.append({"id": model_id, "label": label.strip() or model_id})
    return models


def _fetch_agy_models() -> list[dict]:
    try:
        result = subprocess.run(
            ["agy", "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_AGY_MODELS_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return _parse_agy_models(result.stdout or "")


def _refresh_agy_models() -> None:
    """Ask the CLI and store the outcome. A failed call keeps the last good list
    and is not retried for a minute."""
    models = _fetch_agy_models()  # seconds (~5s measured); never under the lock
    with _AGY_MODELS_LOCK:
        if models:
            _agy_models_cache.update(models=models, ok_at=time.monotonic(), failed_at=None)
        else:
            _agy_models_cache["failed_at"] = time.monotonic()
        _agy_models_cache["refreshing"] = False


def _start_agy_refresh() -> None:
    threading.Thread(target=_refresh_agy_models, name="agy-models-refresh", daemon=True).start()


def _agy_cache_view() -> dict:
    """What the cache holds right now. Caller holds _AGY_MODELS_LOCK. `fresh`
    means the last successful lookup is still inside the TTL."""
    cache = _agy_models_cache
    fresh = cache["ok_at"] is not None and time.monotonic() - cache["ok_at"] < _AGY_MODELS_TTL_SECONDS
    return {"models": list(cache["models"]), "fresh": fresh, "refreshing": bool(cache.get("refreshing"))}


def _agy_lookups_disabled() -> bool:
    """AHB_NO_MODEL_PREWARM=1 turns off every background `agy models` lookup, the
    one at startup and the one the first /api/providers call would start. For
    tests and tooling that run the server on a machine with agy installed: they
    must not spawn the real agy, which opens local sockets of its own."""
    return os.environ.get("AHB_NO_MODEL_PREWARM", "").strip().lower() in ("1", "true", "yes")


def _agy_live_models(wait: bool = False) -> dict:
    """The `agy models` list, cached: {"models", "fresh", "refreshing"}.
    Stale-while-revalidate: by default this never blocks -- an expired or empty
    cache returns what it has (possibly [], and then fresh is False) and
    refreshes in the background, so opening the Settings panel is never held up
    by the CLI's network call. `wait=True` refreshes inline instead."""
    if _agy_lookups_disabled():
        with _AGY_MODELS_LOCK:
            return _agy_cache_view()
    now = time.monotonic()
    with _AGY_MODELS_LOCK:
        cache = _agy_models_cache
        view = _agy_cache_view()
        if view["fresh"]:
            return view
        if cache["failed_at"] is not None and now - cache["failed_at"] < _AGY_MODELS_FAILURE_BACKOFF_SECONDS:
            return view
        if not wait:
            if not cache.get("refreshing"):
                cache["refreshing"] = True
                _start_agy_refresh()
            return _agy_cache_view()
        cache["refreshing"] = True
    _refresh_agy_models()
    with _AGY_MODELS_LOCK:
        return _agy_cache_view()


def prewarm_agy_models() -> None:
    """Start the first `agy models` lookup at app startup (no-op without agy),
    so the list is usually ready before the Settings panel is opened (see
    _agy_lookups_disabled for the switch that turns lookups off)."""
    if _agy_lookups_disabled():
        return
    if cli_available("agy"):
        _agy_live_models()


def cli_model_catalog(provider: str, live: bool = True, wait: bool = False) -> dict:
    """Model suggestions for one provider plus how far to trust them:
    {"models": [...], "authoritative": bool, "source": str, "refreshing": bool}.
    `refreshing` means a background `agy models` lookup is in flight, so the
    list may change shortly. `authoritative`
    means the list is the CLI's own current catalog (codex's cache, a live
    `agy models`), so a saved model missing from it is stale. Claude's list is
    only aliases and its --model also takes full model names, and agy's
    built-in fallback list can itself be old -- neither can prove a saved
    model is wrong, so they are never authoritative. `live=False` never starts
    a process; `wait=True` blocks for a fresh `agy models` instead of answering
    from the cache and refreshing in the background (see _agy_live_models)."""
    if provider == "codex":
        models = _codex_known_models()
        return {
            "models": models,
            "authoritative": bool(models),
            "source": "codex-cache" if models else "none",
            "refreshing": False,
        }
    if provider == "claude":
        return {"models": list(_CLAUDE_KNOWN_MODELS), "authoritative": False, "source": "aliases", "refreshing": False}
    if provider == "agy":
        refreshing = False
        if live and cli_available("agy"):
            state = _agy_live_models(wait=wait)
            refreshing = state["refreshing"]
            if state["models"]:
                # Only a lookup that succeeded within the TTL counts as the CLI's
                # current list. An expired or last-good list still makes useful
                # suggestions but must not call a freshly pinned model stale.
                fresh = state["fresh"]
                return {
                    "models": state["models"],
                    "authoritative": fresh,
                    "source": "agy-live" if fresh else "agy-cache",
                    "refreshing": refreshing,
                }
        return {
            "models": list(_AGY_KNOWN_MODELS),
            "authoritative": False,
            "source": "built-in",
            "refreshing": refreshing,
        }
    return {"models": [], "authoritative": False, "source": "none", "refreshing": False}


def model_is_stale(saved_model: str | None, catalog: dict) -> bool:
    """True when `saved_model` is pinned but absent from a catalog that is the
    CLI's own current list -- the typical case being a model the vendor has
    since retired (the CLI then fails every call)."""
    if not saved_model or not catalog.get("authoritative"):
        return False
    return saved_model not in {m["id"] for m in catalog["models"]}


def known_cli_models(provider: str) -> list[dict]:
    """Real, verified model choices to suggest for a CLI-dispatched
    provider's default/override --model field (Settings panel + titlebar),
    replacing a blank free-text field that had no protection against typos
    like an invalid casing/format (e.g. "Sonnet5" -- not a valid alias or
    full model name, silently saved and passed through to every --model
    call until noticed). Always additive to, never a replacement for, free
    text: the frontend renders this as an <input list=...> (datalist), not
    a closed <select>, so an unlisted-but-valid model still works. Never
    starts a process; the endpoint uses cli_model_catalog() for the live
    `agy models` list."""
    return cli_model_catalog(provider, live=False)["models"]


def save_cli_provider_model(provider: str, model: str | None) -> None:
    """Store (or, with an empty/None `model`, remove) one CLI-dispatched
    provider's default --model override -- the Settings panel's "기본
    모델" field for a provider whose CLI is already detected. Locked
    read-modify-write, same posture as touch_registry()."""
    with _CREDENTIALS_LOCK:
        data = _read_all_credentials_data()
        cli_models = data.get("cli_models")
        cli_models = dict(cli_models) if isinstance(cli_models, dict) else {}
        model = (model or "").strip()
        if model:
            cli_models[provider] = model
        else:
            cli_models.pop(provider, None)
        data["cli_models"] = cli_models
        _write_all_credentials_data(data)


def cli_available(provider: str) -> bool:
    return shutil.which(provider) is not None


