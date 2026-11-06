#!/usr/bin/env bash

# Code version: v1.0.0-claude.0

# Launch the stdio iPhone MCP server (macOS only). Standard output is the JSON-RPC
# channel, so this script writes diagnostics to standard error only.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/resolve_python.sh"

# The MCP server uses only the standard library, so the version-only mode is enough.
if ! PYTHON_BIN="$(resolve_python_bin version)"; then
	echo "Python 3.13 or newer not found: ${AGENTIC_CONTEXT_PYTHON:-${CACHELIKES_PYTHON:-host python3}}" >&2
	echo "Install Python 3.13 or newer, or set AGENTIC_CONTEXT_PYTHON to a suitable interpreter." >&2
	exit 1
fi

cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m app.core.iphone.mcp_server "$@"
