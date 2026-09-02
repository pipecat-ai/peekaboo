#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from store.images import changed_fraction, frame_key, signature  # noqa: E402


def terminal(lines: int, cursor: bool = False) -> Image.Image:
    """A fake 1080-wide terminal with ``lines`` rows of text."""
    img = Image.new("RGB", (1080, 675), (30, 30, 36))
    d = ImageDraw.Draw(img)
    for i in range(lines):
        d.rectangle((40, 40 + i * 28, 700, 58 + i * 28), fill=(210, 210, 220))
    if cursor:
        d.rectangle((40, 40 + lines * 28, 52, 58 + lines * 28), fill=(210, 210, 220))
    return img


def test_same_screen_is_unchanged_and_same_key():
    a, b = signature(terminal(5)), signature(terminal(5))
    assert changed_fraction(a, b) == 0.0
    assert frame_key(a) == frame_key(b)


def test_cursor_blink_is_under_threshold():
    a, b = signature(terminal(5)), signature(terminal(5, cursor=True))
    assert changed_fraction(a, b) < 0.002


def test_new_line_of_output_is_a_change():
    a, b = signature(terminal(5)), signature(terminal(6))
    assert changed_fraction(a, b) >= 0.002
    assert frame_key(a) != frame_key(b)


def test_noise_does_not_change_key():
    base = terminal(5)
    noisy = base.copy()
    px = noisy.load()
    for x in range(0, 1080, 7):
        for y in range(0, 675, 11):
            r, g, b = px[x, y]
            px[x, y] = (min(255, r + 3), min(255, g + 3), min(255, b + 3))
    assert frame_key(signature(base)) == frame_key(signature(noisy))
    assert changed_fraction(signature(base), signature(noisy)) < 0.002
