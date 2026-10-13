# Changelog

One line per change, added in the same commit, newest first. The full diff is in git.

## Unreleased

- README: drop the context-keyboard-display pointers (that repo is private now).
- `scripts/check.sh` (compile + every state renders) and a gitleaks pre-commit hook (`scripts/hooks/pre-commit`).
- AGENTS.md (CLAUDE.md imports it): notes for coding agents.

## 2026-09-25

- fix: read OAuth credentials from the Keychain too; refuse to push until the panel URL is set (95838bf)

## 2026-08-26

- Ignore egg-info from editable installs (9fc2eee)
- Capture tool_use/TodoWrite in transcripts, track turn timing in hooks, add packaging (accecfd)

## 2026-08-25

- Fix the aliasing that was in the test card, and raise SS to where it pays (c73866b)
- Shrink the emphasis, spend it on the clock, and settle the sharpness question (93b17c5)
- Size the type from the panel's physics, not by eye (d8378c8)
- Redesign the panel for the screen it actually runs on (b6fc541)
- Add install.sh, README and the state preview (74bbff7)
- Install: private venv, launchd agent, non-destructive hook merge (7acc935)
- Encode baseline JPEG and push it on a tick with backoff (7a57d23)
- Draw the panel: 142x428 layout and a four-pose character (a6c0e5c)
- Read the state Claude Code leaves on disk (00679fb)
- Add project skeleton: MIT license, ignores, Pillow requirement (ffe85da)
