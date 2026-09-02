#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Make the menu bar icon from the Pipecat logo.

    uv run tools/menubar_icon.py ../pipecat/pipecat.png

Writes ``src/macos/assets/menubar{,@2x,@4x}.png``: the cat mark alone, as a
template image (black plus alpha) at 24 x 14 pt, with the strokes fattened so
they read at menu bar size. The logo PNG has an opaque white background, so
the mask comes from ink luminance, not from the file's alpha.

Also writes ``appicon.png``: the logo's black cat on a white rounded square,
for the Dock and Cmd-Tab while the app has a window open.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

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
    original = ink.crop(ink.point(lambda v: 255 if v > 40 else 0).getbbox())
    glyph = original.filter(ImageFilter.MaxFilter(FATTEN))

    w, h = glyph.width + 2 * PAD, glyph.height + 2 * PAD
    mask = Image.new("L", (w, h), 0)
    mask.paste(glyph, (PAD, PAD))
    # The app icon is large enough for the logo's own stroke; no fattening.
    app_mask = Image.new("L", (original.width + 2 * PAD, original.height + 2 * PAD), 0)
    app_mask.paste(original, (PAD, PAD))

    ASSETS.mkdir(parents=True, exist_ok=True)
    for scale, name in ((1, "menubar.png"), (2, "menubar@2x.png"), (4, "menubar@4x.png")):
        size = (WIDTH_PT * scale, round(WIDTH_PT * scale * h / w))
        small = mask.resize(size, Image.Resampling.LANCZOS)
        small = small.point(lambda v: min(255, int((v / 255) ** EDGE_GAMMA * 255)))
        icon = Image.new("RGBA", size, (0, 0, 0, 255))
        icon.putalpha(small)
        icon.save(ASSETS / name)
        print(f"{ASSETS / name}: {size[0]}x{size[1]}")

    # The app icon: the logo as it is, black cat on white, on a macOS-style
    # rounded square with a hairline so it reads on light backgrounds too.
    side = 512
    tile = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (32, 32, side - 32, side - 32), radius=104, fill=(255, 255, 255, 255), outline=(0, 0, 0, 28), width=2
    )
    cat_w = int(side * 0.62)
    cat = app_mask.resize((cat_w, round(cat_w * app_mask.height / app_mask.width)), Image.Resampling.LANCZOS)
    ink = Image.new("RGBA", cat.size, (0, 0, 0, 255))
    ink.putalpha(cat)
    tile.alpha_composite(ink, ((side - cat.width) // 2, (side - cat.height) // 2))
    tile.save(ASSETS / "appicon.png")
    print(f"{ASSETS / 'appicon.png'}: {side}x{side}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "../pipecat/pipecat.png"))
