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

# state, model, effort, 5-hour %, weekly %. Nothing else reaches the panel any more.
CASES = (
    ("working", "Opus 5", "high", 47.0, 82.0),
    ("waiting", "Opus 5", "xhigh", 12.0, 93.0),
    ("idle", "Sonnet 5", "medium", 62.0, 30.0),
    ("offline", None, None, None, None),
)

GAP, TOP = 14, 34


def assert_clear_of_cutouts(state, frame):
    """The top of the physical panel is behind the device's own cutouts.

    Cheap to check and easy to break: any layout edit that grows something above it
    pushes the character back under the hardware, where it is invisible and looks
    like a rendering bug rather than a layout one.
    """
    pixels = frame.load()
    stray = [(x, y) for y in range(ks.DEAD_ZONE) for x in range(frame.width)
             if pixels[x, y] != ks.BG]
    if stray:
        raise SystemExit(
            f"{state}: {len(stray)} pixel(s) drawn in the top {ks.DEAD_ZONE} rows, "
            f"first at {stray[0]}")


def main():
    config = ks.load_config()
    now = time.time()
    frames = []
    for index, (state, model, effort, five, week) in enumerate(CASES):
        status = ks.Status()
        status.state, status.model, status.effort = state, model, effort
        status.five_hour = {"used_percentage": five, "resets_at": now + 8000} if five else None
        status.seven_day = {"used_percentage": week, "resets_at": now + 250000} if week else None
        status.last_activity = now - 12
        frame = ks.render(status, now, config, phase=index * 0.8)
        assert_clear_of_cutouts(state, frame)
        frames.append((state, frame))

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
    print(f"wrote {out} ({sheet.width}x{sheet.height}); top {ks.DEAD_ZONE} rows clear in all {len(frames)} states")


if __name__ == "__main__":
    main()
