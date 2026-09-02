#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""M0: Watch mode. One ``SCStream`` of a window, an app, or a display, logging
every frame's status and whether the picture changed, with the frames hopping
from ScreenCaptureKit's callback thread into an asyncio loop the way the real
source will.

    uv run spikes/stream.py --title "Terminal" --seconds 20
    uv run spikes/stream.py --app "Google Chrome" --seconds 30 --save
    uv run spikes/stream.py --display --fps 0.5

While it runs: cover the window, switch Space, minimize it, hide the app. Each
line shows the status ScreenCaptureKit attached to the frame (complete, idle,
blank, suspended, stopped) and, for complete frames, how much of the picture
moved since the last one. That is where the change gate and the staleness
warning come from.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

import CoreMedia
import objc
import ScreenCaptureKit as SCK
from Foundation import NSObject
from PIL import Image

from common import (
    _await_completion,
    app_filter,
    display_filter,
    find_app,
    find_window,
    frame_status_name,
    list_windows,
    require_screen_recording,
    sample_buffer_status,
    sample_buffer_to_pil,
    shareable_content,
    stream_configuration,
    window_filter,
)
from store.images import changed_fraction, signature  # noqa: E402 - path set in common

OUT_DIR = Path(__file__).parent / "out"

# Same threshold as the change gate in src/processors/gate.py.
CHANGED_THRESHOLD = 0.002


class StreamOutput(NSObject, protocols=[objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")]):
    """Receives sample buffers on ScreenCaptureKit's queue and hands them to
    the asyncio loop. Does nothing else on that thread but copy."""

    def initWithLoop_queue_(self, loop, queue):
        self = objc.super(StreamOutput, self).init()
        if self is None:
            return None
        self._loop = loop
        self._queue = queue
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
        if output_type != SCK.SCStreamOutputTypeScreen:
            return
        status = sample_buffer_status(sample_buffer)
        pts = CoreMedia.CMTimeGetSeconds(CoreMedia.CMSampleBufferGetPresentationTimeStamp(sample_buffer))
        # Copy the pixels here; the buffer is recycled once we return.
        image = sample_buffer_to_pil(sample_buffer) if status == SCK.SCFrameStatusComplete else None
        self._loop.call_soon_threadsafe(self._queue.put_nowait, ("frame", time.monotonic(), pts, status, image))

    def stream_didStopWithError_(self, stream, error):
        self._loop.call_soon_threadsafe(self._queue.put_nowait, ("stopped", time.monotonic(), None, None, str(error)))


async def consume(queue: asyncio.Queue, *, seconds: Optional[float], save: bool, label: str):
    """Log what arrives. Returns a histogram of statuses."""
    counts: dict[str, int] = {}
    last_sig: Optional[bytes] = None
    last_any = time.monotonic()
    started = time.monotonic()
    n_saved = 0
    OUT_DIR.mkdir(exist_ok=True)

    while True:
        remaining = None if seconds is None else seconds - (time.monotonic() - started)
        if remaining is not None and remaining <= 0:
            break
        try:
            kind, t, pts, status, payload = await asyncio.wait_for(queue.get(), timeout=min(remaining or 2.0, 2.0))
        except asyncio.TimeoutError:
            gap = time.monotonic() - last_any
            print(f"  {time.monotonic() - started:6.1f}s  (no frame for {gap:.0f}s)")
            continue

        last_any = t
        elapsed = t - started
        if kind == "stopped":
            print(f"  {elapsed:6.1f}s  stream stopped: {payload}")
            counts["stopped-error"] = counts.get("stopped-error", 0) + 1
            break

        name = frame_status_name(status)
        counts[name] = counts.get(name, 0) + 1
        image: Optional[Image.Image] = payload

        if image is None:
            print(f"  {elapsed:6.1f}s  {name:9s} pts={pts:.2f} (no picture)")
            continue

        sig = signature(image)
        if last_sig is None:
            moved, changed = 1.0, True
        else:
            moved = changed_fraction(sig, last_sig)
            changed = moved >= CHANGED_THRESHOLD
        if changed:
            last_sig = sig
        mark = "CHANGED" if changed else "same"
        print(f"  {elapsed:6.1f}s  {name:9s} pts={pts:.2f} {image.size[0]}x{image.size[1]} moved={moved:.4f} {mark}")
        if save and changed:
            path = OUT_DIR / f"stream-{label}-{n_saved:03d}.png"
            image.save(path)
            n_saved += 1

    return counts


async def main_async(args):
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
    label = label.lower().replace(" ", "-")

    config = stream_configuration(filter, fps=args.fps, max_width=args.width)
    print(f"Stream {config.width()}x{config.height()} at {args.fps} fps for {args.seconds or 'ever'} s")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    output = StreamOutput.alloc().initWithLoop_queue_(loop, queue)

    stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(filter, config, output)
    # A nil queue lets ScreenCaptureKit pick one; we never block on it.
    ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(output, SCK.SCStreamOutputTypeScreen, None, None)
    if not ok:
        sys.exit(f"addStreamOutput failed: {error}")

    _, error = await loop.run_in_executor(
        None, lambda: _await_completion(lambda h: stream.startCaptureWithCompletionHandler_(lambda e: h(None, e)))
    )
    if error is not None:
        sys.exit(f"startCapture failed: {error}")
    print("Capturing. Cover it, switch Space, minimize it. Ctrl-C to stop.\n")

    try:
        counts = await consume(queue, seconds=args.seconds, save=args.save, label=label)
    except (KeyboardInterrupt, asyncio.CancelledError):
        counts = {}
    finally:
        _, error = await loop.run_in_executor(
            None, lambda: _await_completion(lambda h: stream.stopCaptureWithCompletionHandler_(lambda e: h(None, e)))
        )
        if error is not None:
            print(f"stopCapture: {error}")

    print("\nFrames by status:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--title", help="window title substring")
    target.add_argument("--app", help="application name or bundle id substring")
    target.add_argument("--display", action="store_true", help="the main display")
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long (default: Ctrl-C)")
    parser.add_argument("--save", action="store_true", help="save every changed frame under spikes/out/")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
