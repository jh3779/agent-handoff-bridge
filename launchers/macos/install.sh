#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
BRIDGE_ROOT="${SCRIPT_DIR}/../.."

chmod +x "$SCRIPT_DIR/handoff-bridge.command"
chmod +x "$SCRIPT_DIR/install.sh"

cd "$BRIDGE_ROOT"
python3 handoff_bridge.py check

echo
echo "macOS launcher is ready:"
echo "$SCRIPT_DIR/handoff-bridge.command"
echo
echo "Open it from Finder or run:"
echo "$SCRIPT_DIR/handoff-bridge.command"
