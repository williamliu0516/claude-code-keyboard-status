#!/usr/bin/env python3
"""Render the four states side by side for the README, from synthetic data.

    ~/.claude/keyboard-status-venv/bin/python3 docs/make_preview.py

Kept out of keyboard_status.py because it is documentation, not product: the
`--preview` flag renders the *real* current state, which is the useful thing when
you are debugging and the useless thing when you are writing a README.
"""

import importlib.util
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ks", os.path.join(HERE, "..", "keyboard_status.py"))
ks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ks)

from PIL import Image, ImageDraw  # noqa: E402  (after the module is loaded)

CASES = (
    ("working", "claude-code-keyboard-status", "main", "Opus 5", "high",
     "Design and ship the keyboard status pusher", None, 47.0, 82.0),
    ("waiting", "hermes-agent", "feat/router-costs", "Opus 5", "xhigh",
     None, "Claude needs your permission to use Bash", 12.0, 93.0),
    ("idle", "williamliu0516.github.io", "rewrite-0809-integrated-compact", "Sonnet 5", "medium",
     "Hermes agent 长会话成本优化方案调研与落地", None, 62.0, 30.0),
    ("offline", None, None, None, None, None, None, None, None),
)

GAP, TOP = 14, 34


def main():
    config = ks.load_config()
    now = time.time()
    frames = []
    for index, case in enumerate(CASES):
        state, project, branch, model, effort, title, detail, five, week = case
        status = ks.Status()
        status.state, status.project, status.branch = state, project, branch
        status.model, status.effort = model, effort
        status.title, status.detail = title, detail
        status.five_hour = {"used_percentage": five, "resets_at": now + 8000} if five else None
        status.seven_day = {"used_percentage": week, "resets_at": now + 250000} if week else None
        status.last_activity = now - 12
        frames.append((state, ks.render(status, now, config, phase=index * 0.8)))

    width = config["width"]
    sheet = Image.new("RGB", (len(frames) * (width + GAP) + GAP, config["height"] + TOP + GAP), (18, 17, 16))
    label = ks.font(11, 600, rounded=False)
    draw = ImageDraw.Draw(sheet)
    for index, (state, frame) in enumerate(frames):
        x = GAP + index * (width + GAP)
        sheet.paste(frame, (x, TOP))
        ks.write(draw, x + width / 2, TOP - 19, state, label, (150, 143, 133), align="center")
    out = os.path.join(HERE, "states.png")
    sheet.save(out)
    print(f"wrote {out} ({sheet.width}x{sheet.height})")


if __name__ == "__main__":
    main()
