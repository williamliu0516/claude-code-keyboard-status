# claude-code-keyboard-status: notes for coding agents

`keyboard_status.py` shows live Claude Code state on a mechanical keyboard's 142x428 image panel. It installs Claude
Code hooks that record session state, renders a JPEG and POSTs it to the panel. Today its main users are the hooks
and context-keyboard-display, which imports this repo as a library (transcript parsing, fonts, the mascot, JPEG
encoding, the HTTP push): a change here can break that daemon, so check both. Read [README.md](README.md) first.
Claude Code reads this file through CLAUDE.md.

- `scripts/check.sh` must pass (compile, every state renders with `--preview`, the secret scan is on).
- [CHANGELOG.md](CHANGELOG.md) gets one line at the top, in the same commit.
- One pusher per panel: where context-keyboard-display runs, this repo's own daemon stays off (`--uninstall` would
  also remove the hooks that daemon relies on; disable the launchd agent instead).
- `git status` first: leave other agents' changes alone and out of your commits. No AI attribution trailers; a
  pre-commit hook runs gitleaks (`git config core.hooksPath scripts/hooks`); never `--no-verify`.
- No LAN addresses or tokens in tracked files.
