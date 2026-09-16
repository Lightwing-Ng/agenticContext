#!/usr/bin/env bash

# Code version: v1.5.1-codex.1

_python_required_modules() {
	local mode="${1:-version}"
	case "$mode" in
		runtime|test|quality)
			printf '%s\n' flask playwright yt_dlp pyarrow PIL markdown_it tiktoken
			;;
	esac
	case "$mode" in
		test)
			printf '%s\n' pytest
			;;
		quality)
			printf '%s\n' ruff pytest pytest_cov
			;;
	esac
}

_python_platform_candidates() {
	case "$(uname -s 2>/dev/null || true)" in
		Darwin)
			printf '%s\n' \
				"/usr/local/bin/python3" \
				"/opt/homebrew/bin/python3" \
				"/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
			;;
		Linux)
			printf '%s\n' "/usr/local/bin/python3"
			;;
	esac
}

_python_requirements_compatible() {
	local candidate="$1"
	local mode="$2"
	local scripts_dir
	scripts_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
	local checker="$scripts_dir/check_python_requirements.py"
	local requirements="$scripts_dir/../requirements.txt"
	"$candidate" -c \
		'import runpy, sys; checker = sys.argv[1]; sys.argv = sys.argv[1:]; runpy.run_path(checker, run_name="__main__")' \
		"$checker" "$mode" "$requirements"
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
	else
		while IFS= read -r candidate; do
			candidates+=("$candidate")
		done < <(_python_platform_candidates)
	fi

	local checked_candidates=("")
	for candidate in "${candidates[@]}"; do
		if [[ -z "$candidate" || ! -x "$candidate" ]]; then
			continue
		fi
		local checked_candidate
		for checked_candidate in "${checked_candidates[@]}"; do
			if [[ "$candidate" == "$checked_candidate" ]]; then
				continue 2
			fi
		done
		checked_candidates+=("$candidate")
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

		case "$mode" in
			runtime|test|quality)
				local requirements_error
				if ! requirements_error="$(_python_requirements_compatible "$candidate" "$mode" 2>&1)"; then
					requirements_error="${requirements_error//$'\n'/; }"
					printf 'Skipping Python with incompatible %s dependencies: %s (%s)\n' \
						"$mode" "$candidate" "${requirements_error:-requirements check failed}" >&2
					continue
				fi
				;;
		esac

		printf '%s\n' "$candidate"
		return 0
	done

	return 1
}
