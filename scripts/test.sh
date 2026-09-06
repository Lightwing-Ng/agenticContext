#!/usr/bin/env bash

# Code version: v1.1.2-codex.1

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/resolve_python.sh"
TEST_MARK_EXPRESSION="${AGENTIC_CONTEXT_TEST_MARK_EXPRESSION:-${CACHELIKES_TEST_MARK_EXPRESSION:-not live}}"

if ! PYTHON_BIN="$(resolve_python_bin)"; then
	echo "Supported Python 3.13 or newer interpreter not found: ${AGENTIC_CONTEXT_PYTHON:-${CACHELIKES_PYTHON:-host python3}}" >&2
	echo "Run $ROOT_DIR/scripts/setup_python.sh first." >&2
	exit 1
fi

cd "$ROOT_DIR"
export PYTHONDONTWRITEBYTECODE=1

exec "$PYTHON_BIN" -m pytest -q -p no:cacheprovider -m "$TEST_MARK_EXPRESSION" "$@"
