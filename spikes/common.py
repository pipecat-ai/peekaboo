#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Shared bits for the M0 spikes: permissions, the window list, and turning
ScreenCaptureKit's buffers into PIL images.

Everything here is a candidate for ``src/macos/`` once it is proven. Nothing in
``src/`` imports from here.
"""

import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import AppKit
import CoreMedia
import Quartz
import ScreenCaptureKit as SCK
from PIL import Image

# SCContentFilter (and anything else that touches the window server) asserts
# with CGS_REQUIRE_INIT unless the process has an NSApplication. Creating the
# shared one is enough; no run loop is needed for stills or streams.
AppKit.NSApplication.sharedApplication()

# So spikes can reuse the store's change-detection helpers.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Frames are scaled to at most this wide before hashing, storing, or sending
# to a model, same as the transport source.
FRAME_WIDTH = 1080

# How long to wait for a ScreenCaptureKit completion handler.
SCK_TIMEOUT_SECS = 10.0

FRAME_STATUS_NAMES = {
    SCK.SCFrameStatusComplete: "complete",
    SCK.SCFrameStatusIdle: "idle",
    SCK.SCFrameStatusBlank: "blank",
    SCK.SCFrameStatusSuspended: "suspended",
    SCK.SCFrameStatusStarted: "started",
    SCK.SCFrameStatusStopped: "stopped",
}


def frame_status_name(status: Optional[int]) -> str:
    if status is None:
        return "none"
    return FRAME_STATUS_NAMES.get(status, f"unknown({status})")


#
# Permissions
#


def screen_recording_granted() -> bool:
    """Whether this process may capture the screen. Does not prompt."""
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def request_screen_recording() -> bool:
    """Prompt for Screen Recording if not granted. Returns the grant state.

    The grant attaches to the responsible process: from a terminal that is
    the terminal app, and a relaunch is needed after granting.
    """
    if screen_recording_granted():
        return True
    return bool(Quartz.CGRequestScreenCaptureAccess())


SCREEN_RECORDING_PANE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
)
MICROPHONE_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"


def require_screen_recording():
    if not request_screen_recording():
        sys.exit(
            "Screen Recording is not granted to this process. Enable it for your "
            f"terminal in System Settings ({SCREEN_RECORDING_PANE}) and relaunch the terminal."
        )


#
# Shareable content
#


@dataclass
class WindowInfo:
    id: int
    title: str
    app_name: str
    bundle_id: str
    pid: int
    frame: tuple[float, float, float, float]  # x, y, w, h in points
    on_screen: bool
    layer: int
    sc_window: object  # SCWindow, kept for building filters

    def __str__(self):
        x, y, w, h = self.frame
        flags = ("" if self.on_screen else " offscreen") + (f" layer={self.layer}" if self.layer else "")
        return f"[{self.id}] {self.app_name}: {self.title!r} {int(w)}x{int(h)}@{int(x)},{int(y)}{flags}"


def _await_completion(start, timeout: float = SCK_TIMEOUT_SECS):
    """Run ``start(handler)`` where ``handler(result, error)`` is a completion
    handler, and wait for it. Returns ``(result, error)``."""
    done = threading.Event()
    out: list = [None, None]

    def handler(result, error):
        out[0], out[1] = result, error
        done.set()

    start(handler)
    if not done.wait(timeout):
        raise TimeoutError(f"ScreenCaptureKit did not answer within {timeout}s")
    return out[0], out[1]


def shareable_content(*, on_screen_only: bool = False):
    """The SCShareableContent snapshot: displays, apps, windows."""
    content, error = _await_completion(
        lambda h: SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, on_screen_only, h
        )
    )
    if error is not None:
        raise RuntimeError(f"SCShareableContent failed: {error}")
    return content


def list_windows(content=None, *, min_size: int = 0) -> list[WindowInfo]:
    content = content or shareable_content()
    windows = []
    for w in content.windows():
        app = w.owningApplication()
        frame = w.frame()
        info = WindowInfo(
            id=int(w.windowID()),
            title=str(w.title() or ""),
            app_name=str(app.applicationName()) if app else "",
            bundle_id=str(app.bundleIdentifier()) if app else "",
            pid=int(app.processID()) if app else 0,
            frame=(frame.origin.x, frame.origin.y, frame.size.width, frame.size.height),
            on_screen=bool(w.isOnScreen()),
            layer=int(w.windowLayer()),
            sc_window=w,
        )
        if min_size and (info.frame[2] < min_size or info.frame[3] < min_size):
            continue
        windows.append(info)
    return windows


def find_window(
    windows: list[WindowInfo], *, title: Optional[str] = None, app: Optional[str] = None
) -> Optional[WindowInfo]:
    """First normal-layer window whose title or app name contains the text,
    case-insensitively. Biggest first, so a tiny helper window does not win."""
    title = (title or "").lower()
    app = (app or "").lower()
    matches = [
        w
        for w in windows
        if w.layer == 0
        and (not title or title in w.title.lower())
        and (not app or app in w.app_name.lower() or app in w.bundle_id.lower())
    ]
    matches.sort(key=lambda w: w.frame[2] * w.frame[3], reverse=True)
    return matches[0] if matches else None


def find_app(content, name: str):
    """The SCRunningApplication whose name or bundle id contains ``name``."""
    name = name.lower()
    for app in content.applications():
        if name in str(app.applicationName()).lower() or name in str(app.bundleIdentifier()).lower():
            return app
    return None


#
# Filters and configuration
#


def window_filter(window: WindowInfo):
    """A filter for one window, wherever it is: covered, other Space, off screen."""
    return SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window.sc_window)


def app_filter(content, app, display=None):
    """A filter for every window of one application on a display."""
    display = display or content.displays()[0]
    return SCK.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(
        display, [app], []
    )


def display_filter(content, display=None, excluding_apps=()):
    display = display or content.displays()[0]
    return SCK.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
        display, list(excluding_apps), []
    )


def scaled_size(filter, max_width: int = FRAME_WIDTH) -> tuple[int, int]:
    """Output size in pixels for a filter's content, capped at ``max_width``."""
    rect = filter.contentRect()
    scale = float(filter.pointPixelScale())
    w = max(1.0, rect.size.width * scale)
    h = max(1.0, rect.size.height * scale)
    if w > max_width:
        h = h * max_width / w
        w = max_width
    return int(round(w)), int(round(h))


def stream_configuration(filter, *, fps: float = 1.0, max_width: int = FRAME_WIDTH):
    """Configuration for a still or a stream of a filter's content."""
    config = SCK.SCStreamConfiguration.alloc().init()
    w, h = scaled_size(filter, max_width)
    config.setWidth_(w)
    config.setHeight_(h)
    config.setPixelFormat_(Quartz.kCVPixelFormatType_32BGRA)
    config.setShowsCursor_(False)
    config.setCapturesAudio_(False)
    config.setQueueDepth_(3)
    if fps > 0:
        config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, int(fps)) if fps >= 1 else CoreMedia.CMTimeMake(int(1 / fps), 1))
    return config


#
# Images
#


def cgimage_to_pil(image) -> Image.Image:
    """A CGImage (as SCScreenshotManager returns) to RGB."""
    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)
    bpr = Quartz.CGImageGetBytesPerRow(image)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
    raw = bytes(data)
    # ScreenCaptureKit hands back 32-bit little-endian BGRA with premultiplied alpha.
    return Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", bpr, 1).convert("RGB")


def sample_buffer_status(sample_buffer) -> Optional[int]:
    """The SCFrameStatus attached to a stream sample buffer."""
    attachments = CoreMedia.CMSampleBufferGetSampleAttachmentsArray(sample_buffer, False)
    if not attachments:
        return None
    status = attachments[0].get(SCK.SCStreamFrameInfoStatus)
    return int(status) if status is not None else None


def sample_buffer_to_pil(sample_buffer) -> Optional[Image.Image]:
    """The picture in a stream sample buffer, or None if it carries none
    (idle frames often don't)."""
    pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
    if pixel_buffer is None:
        return None
    width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
    height = Quartz.CVPixelBufferGetHeight(pixel_buffer)
    bpr = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
    Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
    try:
        base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
        if base is None:
            return None
        raw = bytes(base.as_buffer(bpr * height))
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
    return Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", bpr, 1).convert("RGB")
