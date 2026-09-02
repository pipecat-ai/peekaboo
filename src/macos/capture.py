#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""ScreenCaptureKit: the window list, stills, and streams, as asyncio calls.

Facts this code leans on, all verified in the M0 spikes (see spikes/README.md):

- An ``NSApplication`` must exist or ``SCContentFilter`` asserts. No run loop
  is needed for stills or streams.
- Every delivered stream frame is ``complete`` whether or not the picture
  changed; ``idle`` never arrives. Minimizing or hiding the target delivers
  one ``suspended`` frame with no picture, then nothing.
- Occlusion-aware apps (Chrome) on another Space produce ``complete`` frames
  and stills whose content area is blank. Status is not freshness.
- Pixels are 32-bit BGRA; ``bytesPerRow`` is the stride.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

import AppKit
import CoreMedia
import objc
import Quartz
import ScreenCaptureKit as SCK
from Foundation import NSObject
from loguru import logger
from PIL import Image

# Connects the process to the window server. Required before any filter.
AppKit.NSApplication.sharedApplication()

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
# Completion handlers
#


async def _completion(start: Callable, timeout: float = SCK_TIMEOUT_SECS):
    """Run ``start(handler)`` where ``handler(result, error)`` is a
    ScreenCaptureKit completion handler; return ``(result, error)``."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def handler(result, error):
        if not future.done():
            loop.call_soon_threadsafe(future.set_result, (result, error))

    start(handler)
    return await asyncio.wait_for(future, timeout)


#
# Shareable content
#


async def shareable_content(*, on_screen_only: bool = False):
    """The ``SCShareableContent`` snapshot: displays, apps, windows."""
    content, error = await _completion(
        lambda h: SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, on_screen_only, h
        )
    )
    if error is not None:
        raise RuntimeError(f"SCShareableContent failed: {error}")
    return content


#
# Filters and configuration
#


def window_filter(sc_window):
    """One window wherever it is: covered, another Space, off screen."""
    return SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(sc_window)


def app_filter(display, sc_app):
    """Every window of one app on a display."""
    return SCK.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(
        display, [sc_app], []
    )


def display_filter(display, excluding_apps=()):
    return SCK.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
        display, list(excluding_apps), []
    )


def scaled_size(filter, max_width: int) -> tuple[int, int]:
    """Output size in pixels for a filter's content, capped at ``max_width``."""
    rect = filter.contentRect()
    scale = float(filter.pointPixelScale())
    w = max(1.0, rect.size.width * scale)
    h = max(1.0, rect.size.height * scale)
    if w > max_width:
        h = h * max_width / w
        w = max_width
    return int(round(w)), int(round(h))


def stream_configuration(filter, *, max_width: int, fps: float = 0.0):
    """Configuration for a still (``fps=0``) or a stream of a filter's content."""
    config = SCK.SCStreamConfiguration.alloc().init()
    w, h = scaled_size(filter, max_width)
    config.setWidth_(w)
    config.setHeight_(h)
    config.setPixelFormat_(Quartz.kCVPixelFormatType_32BGRA)
    config.setShowsCursor_(False)
    config.setCapturesAudio_(False)
    config.setQueueDepth_(3)
    if fps > 0:
        # CMTime(value, timescale) = value / timescale seconds per frame.
        if fps >= 1:
            config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, int(fps)))
        else:
            config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(int(round(1 / fps)), 1))
    return config


#
# Images
#


def cgimage_to_pil(image) -> Image.Image:
    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)
    bpr = Quartz.CGImageGetBytesPerRow(image)
    raw = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image)))
    return Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", bpr, 1).convert("RGB")


def sample_buffer_status(sample_buffer) -> Optional[int]:
    attachments = CoreMedia.CMSampleBufferGetSampleAttachmentsArray(sample_buffer, False)
    if not attachments:
        return None
    status = attachments[0].get(SCK.SCStreamFrameInfoStatus)
    return int(status) if status is not None else None


def sample_buffer_to_pil(sample_buffer) -> Optional[Image.Image]:
    """The picture in a stream sample buffer, or None when it carries none."""
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


def blank_fraction(image: Image.Image, *, tolerance: int = 8) -> float:
    """How much of the picture is one flat colour.

    An occlusion-aware app that has stopped drawing leaves its content area
    a single colour under the title bar. Measured on a small grayscale copy
    against the most common value; a terminal showing a bare prompt scores
    high too, so this is a hint for staleness, not proof.
    """
    small = image.convert("L").resize((64, 40))
    histogram = small.histogram()
    mode = max(range(256), key=histogram.__getitem__)
    lo, hi = max(0, mode - tolerance), min(255, mode + tolerance)
    near = sum(histogram[lo : hi + 1])
    return near / (small.width * small.height)


#
# Stills
#


async def take_still(filter, config) -> Image.Image:
    """One picture of the filter's content, via ``SCScreenshotManager``."""
    image, error = await _completion(
        lambda h: SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            filter, config, h
        )
    )
    if error is not None:
        raise RuntimeError(f"screenshot failed: {error}")
    return await asyncio.to_thread(cgimage_to_pil, image)


#
# Streams
#


@dataclass
class StreamFrame:
    """What a stream delivered: a status, and a picture when there was one."""

    status: Optional[int]
    image: Optional[Image.Image]
    pts: float
    """Presentation time, seconds on the host clock."""

    @property
    def status_name(self) -> str:
        return frame_status_name(self.status)


class _StreamOutput(
    NSObject,
    protocols=[objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")],
):
    """Receives sample buffers on ScreenCaptureKit's queue. Copies the pixels
    and hops into the asyncio loop; nothing else happens on that thread."""

    def initWithLoop_onFrame_onStop_(self, loop, on_frame, on_stop):
        self = objc.super(_StreamOutput, self).init()
        if self is None:
            return None
        self._loop = loop
        self._on_frame = on_frame
        self._on_stop = on_stop
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
        if output_type != SCK.SCStreamOutputTypeScreen:
            return
        status = sample_buffer_status(sample_buffer)
        pts = CoreMedia.CMTimeGetSeconds(CoreMedia.CMSampleBufferGetPresentationTimeStamp(sample_buffer))
        image = sample_buffer_to_pil(sample_buffer) if status == SCK.SCFrameStatusComplete else None
        self._loop.call_soon_threadsafe(self._on_frame, StreamFrame(status=status, image=image, pts=pts))

    def stream_didStopWithError_(self, stream, error):
        self._loop.call_soon_threadsafe(self._on_stop, str(error) if error is not None else None)


class FrameStream:
    """An ``SCStream`` of one filter, delivering :class:`StreamFrame` to a
    callback on the asyncio loop.

    Args:
        filter: The content to capture.
        config: From :func:`stream_configuration` with ``fps`` set.
        on_frame: Called on the loop with every frame, picture or not.
        on_stop: Called on the loop if the stream stops on its own, with
            the error text or None.
    """

    def __init__(
        self,
        filter,
        config,
        *,
        on_frame: Callable[[StreamFrame], None],
        on_stop: Optional[Callable[[Optional[str]], None]] = None,
    ):
        self._filter = filter
        self._config = config
        self._on_frame = on_frame
        self._on_stop = on_stop or (lambda error: None)
        self._stream = None
        self._output = None

    @property
    def running(self) -> bool:
        return self._stream is not None

    async def start(self):
        if self._stream:
            return
        loop = asyncio.get_running_loop()
        self._output = _StreamOutput.alloc().initWithLoop_onFrame_onStop_(loop, self._on_frame, self._on_stop)
        stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(self._filter, self._config, self._output)
        # A nil queue lets ScreenCaptureKit pick one; we never block on it.
        ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, SCK.SCStreamOutputTypeScreen, None, None
        )
        if not ok:
            raise RuntimeError(f"addStreamOutput failed: {error}")
        _, error = await _completion(lambda h: stream.startCaptureWithCompletionHandler_(lambda e: h(None, e)))
        if error is not None:
            raise RuntimeError(f"startCapture failed: {error}")
        self._stream = stream
        logger.debug(f"stream started: {self._config.width()}x{self._config.height()}")

    async def stop(self):
        if not self._stream:
            return
        stream, self._stream = self._stream, None
        _, error = await _completion(lambda h: stream.stopCaptureWithCompletionHandler_(lambda e: h(None, e)))
        if error is not None:
            logger.debug(f"stopCapture: {error}")
        self._output = None
