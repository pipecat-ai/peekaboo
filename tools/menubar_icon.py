#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Make the menu bar icon from the Pipecat logo.

    uv run tools/menubar_icon.py ../pipecat/pipecat.png

Writes ``src/macos/assets/menubar{,@2x,@4x}.png``: the cat mark alone, as a
template image (black plus alpha) at 20 x 12 pt, with the strokes fattened so
they read at menu bar size. The logo PNG has an opaque white background, so
the mask comes from ink luminance, not from the file's alpha.
"""

import sys
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

ASSETS = Path(__file__).resolve().parents[1] / "src" / "macos" / "assets"

# The wordmark starts past this column; everything left of it is the cat.
CAT_RIGHT_EDGE = 470
# Grows every stroke by (size - 1) / 2 source pixels on each side. The original
# stroke is about 22 px on a 332 px wide glyph.
FATTEN = 9
PAD = 8
WIDTH_PT = 24
# Pushes mid alphas up after downscaling so edges read as ink, not grey.
EDGE_GAMMA = 0.7


def main(logo: Path):
    im = Image.open(logo).convert("RGBA")
    flat = Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im).convert("L")
    ink = ImageOps.invert(flat).crop((0, 0, CAT_RIGHT_EDGE, im.height))
    glyph = ink.crop(ink.point(lambda v: 255 if v > 40 else 0).getbbox())
    glyph = glyph.filter(ImageFilter.MaxFilter(FATTEN))

    w, h = glyph.width + 2 * PAD, glyph.height + 2 * PAD
    mask = Image.new("L", (w, h), 0)
    mask.paste(glyph, (PAD, PAD))

    ASSETS.mkdir(parents=True, exist_ok=True)
    for scale, name in ((1, "menubar.png"), (2, "menubar@2x.png"), (4, "menubar@4x.png")):
        size = (WIDTH_PT * scale, round(WIDTH_PT * scale * h / w))
        small = mask.resize(size, Image.Resampling.LANCZOS)
        small = small.point(lambda v: min(255, int((v / 255) ** EDGE_GAMMA * 255)))
        icon = Image.new("RGBA", size, (0, 0, 0, 255))
        icon.putalpha(small)
        icon.save(ASSETS / name)
        print(f"{ASSETS / name}: {size[0]}x{size[1]}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "../pipecat/pipecat.png"))
