"""Provider API-key storage for API-key mode (Phase 4, DEC-13~15): a
provider with no local CLI installed can still be reached over its vendor
HTTP API directly if the user pastes a key here. Lives at
AUTO_WORKSPACE_BASE_DIR/credentials.json (DEC-14) -- the same "the app
owns this" location Phase 3 established for registry.json, not an OS
keychain or the third-party `keyring` package. File mode restricted to
0600 on write. See webui_api_key_mode.py for what actually calls out with
these keys.

Also stores user-defined **custom providers** (DEC-26): an arbitrary
OpenAI- or Anthropic-compatible HTTP endpoint (a paid aggregator like
OpenRouter, a local server like Ollama/LM Studio, a company gateway --
anything speaking one of those two request/response shapes), for users
who buy API tokens directly rather than installing a vendor CLI, or who
want a model none of codex/claude/gemini cover. Stored under the same
credentials.json's "custom_providers" key so there is still exactly one
file/lock to reason about, keyed by a user-chosen name rather than a
fixed provider string -- see CUSTOM_PROVIDER_PREFIX for how that name
becomes a `provider` value everywhere else in this codebase.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path

from handoff_bridge import atomic_write_text

import webui_common

_CREDENTIALS_LOCK = threading.Lock()

# A custom provider is identified everywhere else (chat records, /api/run's
# `provider` field, webui_bridge_run.py's dispatch) as this prefix plus the
# user-chosen name, e.g. "custom:openrouter" -- unambiguous against the
# fixed codex/claude/gemini strings without needing a second "kind" field
# threaded through every function that currently just takes `provider: str`.
CUSTOM_PROVIDER_PREFIX = "custom:"
CUSTOM_PROVIDER_API_FORMATS = ("openai", "anthropic")
_CUSTOM_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


def is_custom_provider(provider: str) -> bool:
    return provider.startswith(CUSTOM_PROVIDER_PREFIX)


def custom_provider_name(provider: str) -> str:
    return provider[len(CUSTOM_PROVIDER_PREFIX) :]


def custom_provider_id(name: str) -> str:
    return f"{CUSTOM_PROVIDER_PREFIX}{name}"


def validate_custom_provider_name(raw_name: str) -> str:
    """Raises ValueError with a client-facing message on an invalid name."""
    name = (raw_name or "").strip()
    if not name:
        raise ValueError("custom provider name is required")
    if not _CUSTOM_PROVIDER_NAME_RE.match(name):
        raise ValueError(
            "custom provider name must be 1-40 characters, start with a letter or digit, "
            "and use only letters, digits, '-', or '_'"
        )
    if name in API_KEY_MODE_PROVIDERS:
        raise ValueError(f"'{name}' is a built-in provider name -- choose a different name for a custom provider")
    return name


# DEC-15 row. DEC-25 resolves that question: yes, via call_gemini_api()
# below. Kept as its own tuple rather than an alias for `PROVIDERS`
# (imported from handoff_bridge above) even though the two now happen to
# be equal -- a *future* provider added to PROVIDERS for CLI dispatch
# must not silently gain API-key mode without its own deliberate decision
# the same way Gemini just did. `cli_available()`-based dispatch and the
# Diagnose panel's CLI-detection badges use the full `PROVIDERS`;
# credential storage, `/api/provider-key`, and `API_KEY_MODE_DEFAULT_MODELS`
# use this one instead.
API_KEY_MODE_PROVIDERS = ("codex", "claude", "gemini")


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
    """The raw parsed file: fixed-provider entries plus the
    "custom_providers" key, both read_credentials() and
    read_custom_providers() slice their own view out of this. Never
    raises -- a missing, corrupt, or unreadable (permissions) file just
    means nothing is configured yet, same posture as read_registry()."""
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all_credentials_data(data: dict) -> None:
    path = credentials_path()
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort on platforms/filesystems that don't support chmod


def read_credentials() -> dict:
    """Provider name -> {"key": str, "model": str|None}, for the fixed
    codex/claude/gemini providers only -- read_custom_providers() below
    is the equivalent for user-defined ones. Never raises, same posture
    as _read_all_credentials_data()."""
    data = _read_all_credentials_data()
    result = {}
    for provider, entry in data.items():
        if provider not in API_KEY_MODE_PROVIDERS or not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            continue
        model = entry.get("model")
        result[provider] = {"key": key, "model": model if isinstance(model, str) and model else None}
    return result


def save_credential(provider: str, key: str, model: str | None) -> None:
    """Store (or, with an empty `key`, remove) one provider's API key.

    Locked and read-modify-write, like touch_registry() -- this file can be
    written from multiple request threads (two browser tabs opening the
    connection panel at once)."""
    with _CREDENTIALS_LOCK:
        data = _read_all_credentials_data()
        if key:
            data[provider] = {"key": key, "model": model or None}
        else:
            data.pop(provider, None)
        _write_all_credentials_data(data)


def read_custom_providers() -> dict:
    """name -> {"key","model","base_url","api_format"}. Same never-raises
    posture as read_credentials(); a malformed individual entry is
    skipped, not fatal to the rest."""
    raw = _read_all_credentials_data().get("custom_providers")
    if not isinstance(raw, dict):
        return {}
    result = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        key, model, base_url, api_format = entry.get("key"), entry.get("model"), entry.get("base_url"), entry.get("api_format")
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(base_url, str) or not base_url:
            continue
        if api_format not in CUSTOM_PROVIDER_API_FORMATS:
            continue
        result[name] = {
            "key": key,
            "model": model if isinstance(model, str) and model else None,
            "base_url": base_url,
            "api_format": api_format,
        }
    return result


def save_custom_provider(raw_name: str, key: str, model: str | None, base_url: str, api_format: str) -> None:
    """Store (or, with an empty `key`, remove) one custom provider entry.
    Same locked read-modify-write posture as save_credential(). Raises
    ValueError (client-facing message, caught by the HTTP layer same as
    WorkspaceError elsewhere) on an invalid name/format/url/missing model
    -- but only when actually saving a key; deleting (`key` falsy) only
    needs a valid *name* to know what to remove."""
    name = validate_custom_provider_name(raw_name)
    base_url = (base_url or "").strip().rstrip("/")
    if key:
        if api_format not in CUSTOM_PROVIDER_API_FORMATS:
            raise ValueError(f"api_format must be one of: {', '.join(CUSTOM_PROVIDER_API_FORMATS)}")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if not model:
            raise ValueError("model is required for a custom provider")
    with _CREDENTIALS_LOCK:
        data = _read_all_credentials_data()
        custom = data.get("custom_providers")
        custom = dict(custom) if isinstance(custom, dict) else {}
        if key:
            custom[name] = {"key": key, "model": model, "base_url": base_url, "api_format": api_format}
        else:
            custom.pop(name, None)
        data["custom_providers"] = custom
        _write_all_credentials_data(data)


def delete_custom_provider(raw_name: str) -> None:
    save_custom_provider(raw_name, "", None, "", "")


def cli_available(provider: str) -> bool:
    return shutil.which(provider) is not None


