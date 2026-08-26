#!/bin/sh
# One-command install for the Claude Code keyboard status display.
#
#   curl -fsSL https://raw.githubusercontent.com/williamliu0516/claude-code-keyboard-status/main/install.sh | sh
#
# Downloads keyboard_status.py to ~/.claude/, builds a private virtualenv with
# Pillow in it, loads a launchd agent that keeps the pusher running, and registers
# five session hooks in ~/.claude/settings.json -- preserving every other setting
# and every other tool's hooks. Safe to re-run; that is also how you upgrade.
#
# Pass --no-hooks to install the daemon alone and leave settings.json untouched.
# Set CLAUDE_KEYBOARD_SOURCE to install from somewhere else (a fork, a file:// path).
set -eu

MIRRORS="${CLAUDE_KEYBOARD_SOURCE:-}"
if [ -z "$MIRRORS" ]; then
	MIRRORS="https://raw.githubusercontent.com/williamliu0516/claude-code-keyboard-status/main/keyboard_status.py
https://xiaweiliu.com/claude-code-keyboard-status/keyboard_status.py"
fi

fail() {
	printf 'install: %s\n' "$*" >&2
	exit 1
}

command -v python3 >/dev/null 2>&1 || fail "python3 is required. Install it and re-run."

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' ||
	fail "python3 3.8 or newer is required (found $(python3 -V 2>&1))."

python3 -c 'import venv' 2>/dev/null ||
	fail "python3 is missing the venv module. On Debian/Ubuntu: apt install python3-venv."

# Mirror failures are expected -- we fall through to the next one -- so their
# diagnostics are suppressed. Only the final "no mirror worked" message is shown.
if command -v curl >/dev/null 2>&1; then
	fetch() { curl -fsSL "$1" -o "$2" 2>/dev/null; }
elif command -v wget >/dev/null 2>&1; then
	fetch() { wget -qO "$2" "$1" 2>/dev/null; }
else
	fail "need curl or wget to download the pusher."
fi

tmp="$(mktemp "${TMPDIR:-/tmp}/keyboard-status.XXXXXX")" || fail "cannot create a temporary file."
trap 'rm -f "$tmp"' EXIT INT TERM

got=""
for source in $MIRRORS; do
	fetch "$source" "$tmp" || continue
	# A captive portal or a redirect to a login page answers 200 with HTML, which
	# curl -f cannot catch. Refuse to install anything that is not the script.
	if head -n 1 "$tmp" | grep -q '^#!/usr/bin/env python3'; then
		got="$source"
		break
	fi
done

[ -n "$got" ] || fail "could not download keyboard_status.py from any of:
$MIRRORS
If the repository is private, make it public or set CLAUDE_KEYBOARD_SOURCE."

printf 'install: fetched %s\n' "$got"
python3 "$tmp" --install "$@"
