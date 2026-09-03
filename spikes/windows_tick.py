#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""M7 spike: what does one recording tick cost when every window is captured?

Each tick takes a still of the display plus a still of every capturable
window (regular apps, content windows), hashes them, and reports how long the
tick took, how many bytes it would store, how many windows changed since the
last tick, and how many came back blank (occlusion-aware apps on another
Space). Run it while working normally:

    uv run spikes/windows_tick.py --ticks 6 --every 5
"""

import argparse
import asyncio
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from macos.capture import (  # noqa: E402
    blank_fraction,
    display_filter,
    shareable_content,
    stream_configuration,
    take_still,
    window_filter,
)
from macos.registry import WindowRegistry  # noqa: E402

WINDOW_WIDTH = 1080
SCREEN_WIDTH = 1280
BLANK = 0.97


def dhash(image: Image.Image) -> int:
    small = image.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def jpeg_bytes(image: Image.Image, quality: int = 80) -> int:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getbuffer().nbytes


async def tick(registry: WindowRegistry, last: dict[int, int], own_pid: int) -> dict:
    t0 = time.perf_counter()
    content = await shareable_content()
    display = content.displays()[0]
    screen = await take_still(display_filter(display), stream_configuration(display_filter(display), max_width=SCREEN_WIDTH))
    t_screen = time.perf_counter() - t0
    screen_bytes = jpeg_bytes(screen)

    windows = [w for w in registry.windows if w.pid != own_pid]
    rows = []
    for w in windows:
        sc = registry.sc_window(w.id)
        if sc is None:
            continue
        t1 = time.perf_counter()
        try:
            f = window_filter(sc)
            image = await take_still(f, stream_configuration(f, max_width=WINDOW_WIDTH))
        except Exception as e:  # noqa: BLE001 - reported per window
            rows.append({"app": w.app, "title": w.title, "error": str(e)[:60]})
            continue
        dt = time.perf_counter() - t1
        h = dhash(image)
        changed = last.get(w.id) != h
        last[w.id] = h
        rows.append(
            {
                "app": w.app,
                "title": (w.title or "")[:40],
                "ms": int(dt * 1000),
                "kb": jpeg_bytes(image) // 1024 if changed else 0,
                "changed": changed,
                "blank": blank_fraction(image) >= BLANK,
                "size": image.size,
            }
        )
    return {"t_screen": t_screen, "screen_kb": screen_bytes // 1024, "total": time.perf_counter() - t0, "rows": rows}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=4)
    ap.add_argument("--every", type=float, default=5.0)
    args = ap.parse_args()

    import os

    registry = WindowRegistry()
    await registry.start()
    await asyncio.sleep(1.5)  # first snapshot
    last: dict[int, int] = {}
    for n in range(args.ticks):
        r = await tick(registry, last, os.getpid())
        rows = r["rows"]
        ok = [x for x in rows if "error" not in x]
        changed = [x for x in ok if x["changed"]]
        blank = [x for x in ok if x["blank"]]
        print(
            f"tick {n + 1}: {r['total'] * 1000:.0f} ms total, screen {r['t_screen'] * 1000:.0f} ms / {r['screen_kb']} KB, "
            f"{len(ok)} windows ({sum(x['ms'] for x in ok)} ms), {len(changed)} changed ({sum(x['kb'] for x in changed)} KB), "
            f"{len(blank)} blank, {len(rows) - len(ok)} errors"
        )
        if n == 0 or n == args.ticks - 1:
            for x in ok:
                print(f"    {x['ms']:4d} ms {x['size'][0]}x{x['size'][1]} {'CHANGED' if x['changed'] else '       '} {'BLANK' if x['blank'] else '     '} {x['app']}: {x['title']}")
            for x in rows:
                if "error" in x:
                    print(f"    error {x['app']}: {x['title'][:40]} -> {x['error']}")
        if n < args.ticks - 1:
            await asyncio.sleep(args.every)
    await registry.stop()


if __name__ == "__main__":
    asyncio.run(main())
