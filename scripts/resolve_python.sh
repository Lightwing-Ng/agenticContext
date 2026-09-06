#!/usr/bin/env bash

# Code version: v1.2.0-codex.1

resolve_python_bin() {
	local candidate
	local explicit_candidate="${AGENTIC_CONTEXT_PYTHON:-${CACHELIKES_PYTHON:-}}"
	local candidates=(
		"$(command -v python3 2>/dev/null || true)"
		"$(command -v python 2>/dev/null || true)"
		"/usr/local/bin/python3"
		"/opt/homebrew/bin/python3"
		"/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
	)

	if [[ -n "$explicit_candidate" ]]; then
		candidates=("$explicit_candidate")
	fi

	for candidate in "${candidates[@]}"; do
		if [[ -x "$candidate" ]] && "$candidate" -c \
			'import sys; raise SystemExit(sys.version_info[:2] < (3, 13))' \
			>/dev/null 2>&1; then
			printf '%s\n' "$candidate"
			return 0
		fi
	done

	return 1
}
