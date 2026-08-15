"""Provider API-key storage for API-key mode (Phase 4, DEC-13~15): a
provider with no local CLI installed can still be reached over its vendor
HTTP API directly if the user pastes a key here. Lives at
AUTO_WORKSPACE_BASE_DIR/credentials.json (DEC-14) -- the same "the app
owns this" location Phase 3 established for registry.json, not an OS
keychain or the third-party `keyring` package. File mode restricted to
0600 on write. See webui_api_key_mode.py for what actually calls out with
these keys.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

from handoff_bridge import atomic_write_text

import webui_common

_CREDENTIALS_LOCK = threading.Lock()


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


def read_credentials() -> dict:
    """Provider name -> {"key": str, "model": str|None}. Never raises -- a
    missing, corrupt, or unreadable (permissions) credentials file just
    means no providers are configured yet, same posture as read_registry()."""
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
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
        path = credentials_path()
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        if key:
            data[provider] = {"key": key, "model": model or None}
        else:
            data.pop(provider, None)
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort on platforms/filesystems that don't support chmod


def cli_available(provider: str) -> bool:
    return shutil.which(provider) is not None


