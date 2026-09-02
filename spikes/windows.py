#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""M0: the registry's raw material.

Prints every display, running app, and window ScreenCaptureKit knows about,
including windows that are off screen or on another Space. With ``--poll`` it
diffs the window list once a second and prints open, close, and title-change
events, which is what the registry will do.

    uv run spikes/windows.py
    uv run spikes/windows.py --poll
"""

import argparse
import time

from common import list_windows, require_screen_recording, shareable_content


def dump(min_size: int):
    content = shareable_content()

    print("Displays:")
    for d in content.displays():
        f = d.frame()
        print(f"  [{d.displayID()}] {d.width()}x{d.height()} @ {int(f.origin.x)},{int(f.origin.y)}")

    apps = sorted(content.applications(), key=lambda a: str(a.applicationName()).lower())
    print(f"\nApplications ({len(apps)}):")
    for a in apps:
        print(f"  {a.applicationName()} ({a.bundleIdentifier()}) pid={a.processID()}")

    windows = list_windows(content, min_size=min_size)
    on = [w for w in windows if w.on_screen]
    off = [w for w in windows if not w.on_screen]
    print(f"\nWindows ({len(windows)}: {len(on)} on screen, {len(off)} off screen or on another Space):")
    for w in sorted(windows, key=lambda w: (not w.on_screen, w.app_name.lower(), w.title.lower())):
        print(f"  {w}")


def poll(min_size: int, interval: float):
    """Diff the window list on a cadence: the registry's event source."""
    seen = {w.id: w for w in list_windows(min_size=min_size)}
    print(f"Watching {len(seen)} windows; open, close, or retitle something. Ctrl-C to stop.")
    while True:
        t0 = time.monotonic()
        now = {w.id: w for w in list_windows(min_size=min_size)}
        cost = (time.monotonic() - t0) * 1000
        for wid, w in now.items():
            old = seen.get(wid)
            if old is None:
                print(f"+ opened   {w}")
            elif old.title != w.title:
                print(f"~ retitled {w.app_name}: {old.title!r} -> {w.title!r}")
            elif old.on_screen != w.on_screen:
                print(f"~ {'shown  ' if w.on_screen else 'hidden '} {w}")
        for wid, w in seen.items():
            if wid not in now:
                print(f"- closed   {w}")
        seen = now
        print(f"  (poll took {cost:.0f} ms)", end="\r", flush=True)
        time.sleep(max(0.0, interval - cost / 1000))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--poll", action="store_true", help="diff the window list once a second")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--min-size", type=int, default=50, help="skip windows smaller than this (points)")
    args = parser.parse_args()

    require_screen_recording()
    if args.poll:
        try:
            poll(args.min_size, args.interval)
        except KeyboardInterrupt:
            print()
    else:
        dump(args.min_size)


if __name__ == "__main__":
    main()
