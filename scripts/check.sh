#!/usr/bin/env bash

# Code version: v1.3.2-codex.1

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/resolve_python.sh"
COVERAGE_MINIMUM="${AGENTIC_CONTEXT_COVERAGE_MINIMUM:-${CACHELIKES_COVERAGE_MINIMUM:-55}}"

if ! PYTHON_BIN="$(resolve_python_bin)"; then
	echo "Supported Python 3.13 or newer interpreter not found: ${AGENTIC_CONTEXT_PYTHON:-${CACHELIKES_PYTHON:-host python3}}" >&2
	exit 1
fi

if ! command -v node >/dev/null 2>&1; then
	echo "Node.js is required for JavaScript syntax checks." >&2
	exit 1
fi

if [[ ! "$COVERAGE_MINIMUM" =~ ^[0-9]+$ ]] || (( COVERAGE_MINIMUM < 0 || COVERAGE_MINIMUM > 100 )); then
	echo "AGENTIC_CONTEXT_COVERAGE_MINIMUM must be an integer from 0 to 100." >&2
	exit 1
fi

cd "$ROOT_DIR"
mkdir -p test-results
export COVERAGE_FILE="$ROOT_DIR/test-results/.coverage"
export AGENTIC_CONTEXT_PYTHON="$PYTHON_BIN"

echo "Quality gate configuration: Python=$PYTHON_BIN, branch coverage minimum=${COVERAGE_MINIMUM}%"

echo "[1/5] Python static checks"
"$PYTHON_BIN" -m ruff check main.py app tests scripts

echo "[2/5] Local documentation checks"
"$PYTHON_BIN" scripts/check_docs.py

echo "[3/5] JavaScript syntax checks"
JS_FILE_COUNT=0
while IFS= read -r script_file; do
	JS_FILE_COUNT=$((JS_FILE_COUNT + 1))
	node --check "$script_file"
done < <(find app/web/static -type f -name '*.js' | sort)

if (( JS_FILE_COUNT == 0 )); then
	echo "No first-party JavaScript files were found for syntax checks." >&2
	exit 1
fi

echo "[4/5] JavaScript unit tests"
node --test tests/test_agent_optimization.mjs tests/test_beta_engines.mjs tests/test_select_controller.mjs

echo "[5/5] Python tests with branch coverage"
COVERAGE_STARTED_AT="$(mktemp "$ROOT_DIR/test-results/coverage-start.XXXXXX")"
trap 'rm -f "$COVERAGE_STARTED_AT"' EXIT
"$ROOT_DIR/scripts/test.sh" \
	--cov=app \
	--cov-branch \
	--cov-report=term-missing \
	--cov-report=json:test-results/coverage.json \
	--cov-fail-under="$COVERAGE_MINIMUM"

if [[ ! -f test-results/coverage.json || ! test-results/coverage.json -nt "$COVERAGE_STARTED_AT" ]]; then
	echo "Pytest did not produce a fresh coverage report." >&2
	exit 1
fi

echo "Quality gate passed."
