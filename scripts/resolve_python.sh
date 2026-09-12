#!/usr/bin/env bash

# Code version: v1.4.0-codex.1

_python_required_modules() {
	case "${1:-version}" in
		runtime)
			printf '%s\n' flask playwright yt_dlp pyarrow PIL markdown_it
			;;
		test)
			printf '%s\n' pytest
			;;
		quality)
			printf '%s\n' ruff pytest pytest_cov
			;;
	esac
}

resolve_python_bin() {
	local mode="${1:-version}"
	local candidate
	local explicit_candidate="${AGENTIC_CONTEXT_PYTHON:-${CACHELIKES_PYTHON:-}}"
	local candidates=(
		"$(command -v python3 2>/dev/null || true)"
		"$(command -v python 2>/dev/null || true)"
	)

	if [[ -n "$explicit_candidate" ]]; then
		candidates=("$explicit_candidate")
	fi

	for candidate in "${candidates[@]}"; do
		if [[ -z "$candidate" || ! -x "$candidate" ]]; then
			continue
		fi
		if ! "$candidate" -c \
			'import sys; raise SystemExit(sys.version_info[:2] < (3, 13))' \
			>/dev/null 2>&1; then
			continue
		fi

		local missing_modules=()
		local module
		while IFS= read -r module; do
			if [[ -n "$module" ]] && ! "$candidate" -c "import $module" >/dev/null 2>&1; then
				missing_modules+=("$module")
			fi
		done < <(_python_required_modules "$mode")

		if (( ${#missing_modules[@]} > 0 )); then
			printf 'Skipping Python with missing %s dependencies: %s (%s)\n' \
				"$mode" "$candidate" "${missing_modules[*]}" >&2
			continue
		fi

		printf '%s\n' "$candidate"
		return 0
	done

	return 1
}
