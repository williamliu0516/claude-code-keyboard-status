#!/bin/bash
# The one check to run before saying a change is done or committing it. There is no test suite: the script must
# compile, and every state must render to a file (--preview pushes nothing). Needs the .venv with Pillow:
# python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
failed=0
bad() { printf '✗ %s\n' "$1" >&2; failed=1; }
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

echo "→ compile"
python3 -m py_compile keyboard_status.py || bad "keyboard_status.py does not compile"
echo "→ render"
for state in "" working waiting idle offline; do
  .venv/bin/python3 keyboard_status.py --preview "$OUT/x.png" $state >/dev/null 2>&1 && [ -s "$OUT/x.png" ] \
    || bad "--preview ${state:-(live state)} renders nothing"
  rm -f "$OUT/x.png"
done
echo "→ hooks"
[ "$(git config core.hooksPath || true)" = "scripts/hooks" ] || bad "the secret scan is off in this clone: git config core.hooksPath scripts/hooks"
command -v gitleaks >/dev/null || bad "gitleaks is not installed (brew install gitleaks)"

if [ "$failed" = 0 ]; then echo "✓ all checks passed"; else echo "✗ some checks failed" >&2; fi
exit "$failed"
