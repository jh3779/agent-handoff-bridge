#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
BRIDGE_ROOT="${SCRIPT_DIR}/../.."
cd "$BRIDGE_ROOT"

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$BRIDGE_ROOT/handoff_desktop.py" "$@"
fi

echo "python3 was not found. Install Python 3, then run this launcher again."
read "?Press Enter to close."
