#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""M0: Look mode. One still of a window, an app, or a display with
``SCScreenshotManager``, no stream.

    uv run spikes/shot.py --title "Terminal"           # window whose title contains this
    uv run spikes/shot.py --app "Google Chrome"        # every window of the app
    uv run spikes/shot.py --display                    # the main display
    uv run spikes/shot.py --title "Terminal" --loop 5  # a still a second, five times

Cover the window with another one, or move it to another Space, and run again:
the still should still show its content. That is the occlusion test.
"""

import argparse
import sys
import time
from pathlib import Path

import ScreenCaptureKit as SCK

from common import (
    _await_completion,
    app_filter,
    cgimage_to_pil,
    display_filter,
    find_app,
    find_window,
    list_windows,
    require_screen_recording,
    shareable_content,
    stream_configuration,
    window_filter,
)

OUT_DIR = Path(__file__).parent / "out"


def take_still(filter, config):
    """One CGImage of the filter's content, as a PIL image, plus how long it took."""
    t0 = time.monotonic()
    image, error = _await_completion(
        lambda h: SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            filter, config, h
        )
    )
    took = time.monotonic() - t0
    if error is not None:
        raise RuntimeError(f"screenshot failed: {error}")
    return cgimage_to_pil(image), took


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--title", help="window title substring")
    target.add_argument("--app", help="application name or bundle id substring")
    target.add_argument("--display", action="store_true", help="the main display")
    parser.add_argument("--width", type=int, default=1080, help="max output width in pixels")
    parser.add_argument("--loop", type=int, default=1, help="take this many stills a second apart")
    parser.add_argument("--out", type=Path, default=None, help="where to save (default spikes/out/shot-*.png)")
    args = parser.parse_args()

    require_screen_recording()
    content = shareable_content()

    if args.display:
        filter = display_filter(content)
        label = "display"
    elif args.app:
        app = find_app(content, args.app)
        if app is None:
            sys.exit(f"no running app matching {args.app!r}")
        filter = app_filter(content, app)
        label = str(app.applicationName())
    else:
        window = find_window(list_windows(content), title=args.title)
        if window is None:
            sys.exit(f"no window with {args.title!r} in its title")
        print(f"Target: {window}")
        filter = window_filter(window)
        label = window.app_name

    config = stream_configuration(filter, fps=0, max_width=args.width)
    rect = filter.contentRect()
    print(
        f"Content rect {int(rect.size.width)}x{int(rect.size.height)} pt, "
        f"scale {filter.pointPixelScale()}, output {config.width()}x{config.height()} px"
    )

    OUT_DIR.mkdir(exist_ok=True)
    for i in range(args.loop):
        image, took = take_still(filter, config)
        out = args.out or OUT_DIR / f"shot-{label.lower().replace(' ', '-')}-{i}.png"
        image.save(out)
        print(f"{image.size[0]}x{image.size[1]} in {took * 1000:.0f} ms -> {out}")
        if i + 1 < args.loop:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
