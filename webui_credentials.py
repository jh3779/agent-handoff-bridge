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
import shutil
import threading
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

# Copied from `agy models` (agy 1.4.8, 2026-10-28), so these are the ids the
# CLI itself accepts for --model today -- including non-Google models, since
# Antigravity runs Claude and open-weight models as well as Gemini. The CLI's
# own list is authoritative and changes over time; this is still a
# <datalist>, not a closed <select>, so a stale/renamed entry here degrades to
# "one suggestion doesn't autofill" rather than "the only options are wrong."
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


def known_cli_models(provider: str) -> list[dict]:
    """Real, verified model choices to suggest for a CLI-dispatched
    provider's default/override --model field (Settings panel + titlebar),
    replacing a blank free-text field that had no protection against typos
    like an invalid casing/format (e.g. "Sonnet5" -- not a valid alias or
    full model name, silently saved and passed through to every --model
    call until noticed). Always additive to, never a replacement for, free
    text: the frontend renders this as an <input list=...> (datalist), not
    a closed <select>, so an unlisted-but-valid model still works."""
    if provider == "codex":
        return _codex_known_models()
    if provider == "claude":
        return list(_CLAUDE_KNOWN_MODELS)
    if provider == "agy":
        return list(_AGY_KNOWN_MODELS)
    return []


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


