#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import ScreenCaptureKit as SCK
from loguru import logger

from macos.capture import (
    FrameStream,
    StreamFrame,
    blank_fraction,
    display_filter,
    stream_configuration,
    take_still,
    window_filter,
)
from macos.registry import EventKind, RegistryEvent, Window, WindowRegistry
from processors.frames import ScreenFrame
from sources.base import BaseFrameSource, Resolved

SCREEN_TARGET = "screen"
WINDOW_PREFIX = "window:"

# The display is captured wider than a window would be, so text stays
# legible after the whole screen is scaled down.
DISPLAY_FRAME_WIDTH = 1280
WINDOW_FRAME_WIDTH = 1080
STREAM_FPS = 1.0

# A streamed window delivers a frame every second whether or not it changed
# (M0). Silence this long means it stopped: minimized, hidden, or gone.
STALE_AFTER_SECS = 4.0

# A frame this uniform has no content. Occlusion-aware apps on another Space
# hand over their chrome with an empty content area; so does a cleared
# terminal, which is why blankness has to persist before it counts.
BLANK_FRACTION = 0.97
BLANK_AFTER_SECS = 10.0

SCREEN_WORDS = {"", "screen", "the screen", "display", "the display", "everything", "my screen"}


@dataclass
class _Streamed:
    """A window target with a stream behind it, and what we know of its health."""

    target: str
    window_id: int
    stream: Optional[FrameStream] = None
    last_frame_at: Optional[float] = None
    blank_since: Optional[float] = None
    stale_reason: Optional[str] = None


class ScreenCaptureSource(BaseFrameSource):
    """The Mac's own screen and windows, read from the OS with ScreenCaptureKit.

    Two kinds of target:

    - ``screen``: the main display, as Record mode wants (plan §9.10). A still
      from ``SCScreenshotManager`` on the cadence and on demand.
    - ``window:<id>``: one window, wherever it is, covered or on another Space.
      Added when a watcher needs it: an ``SCStream`` at 1 fps pushes frames as
      they come. A look at a window that is not watched takes a still.

    Only window filters follow a window across Spaces; the app-level filter is
    scoped to what the display currently shows (M2 spike), so "Chrome" resolves
    to Chrome's front content window rather than to the app.

    Staleness, three ways (plan §9.11): a ``suspended`` frame, no frame for
    :data:`STALE_AFTER_SECS`, or frames that stay blank for
    :data:`BLANK_AFTER_SECS`. Each fires ``on_target_stale`` once with a spoken
    reason; content coming back fires ``on_target_fresh``. A watched window
    closing fires ``on_target_lost`` and removes the target.

    Every frame is tagged with the app and window title behind it.
    """

    def __init__(self, *, registry: WindowRegistry, width: int = DISPLAY_FRAME_WIDTH, **kwargs):
        super().__init__(targets={SCREEN_TARGET}, **kwargs)
        self._registry = registry
        self._width = width
        self._filters: dict[str, tuple] = {}
        self._streamed: dict[str, _Streamed] = {}
        self._monitor: Optional[asyncio.Task] = None
        # Targets with a still in flight. A second request while one is
        # underway is dropped; whoever asked gets the frame on its way.
        self._busy: set[str] = set()
        # Targets whose last still failed, so a persistent failure (the
        # screen is locked, say) is one warning, not one a second.
        self._failing: set[str] = set()
        registry.on_event(self._on_registry_event)

    #
    # Targets
    #

    def resolve(self, text: Optional[str]) -> Resolved:
        query = (text or "").strip()
        if query.lower() in SCREEN_WORDS:
            return Resolved(SCREEN_TARGET, "the screen", exact=True)

        window = self._registry.find_window(query)
        if window is None:
            app = self._registry.find_app(query)
            if app is not None:
                window = self._front_window_of(app.pid)
        if window is None:
            return Resolved(SCREEN_TARGET, "the screen", exact=False)
        return Resolved(self._target_for(window), self._label_for(window), exact=True)

    def label(self, target: str) -> str:
        if target == SCREEN_TARGET:
            return "the screen"
        window = self._window_for(target)
        return self._label_for(window) if window else "that window"

    async def add_target(self, target: str):
        await super().add_target(target)
        if target == SCREEN_TARGET or target in self._streamed:
            return
        window = self._window_for(target)
        if window is None:
            raise ValueError(f"{target} is not a known window")
        sc_window = self._registry.sc_window(window.id)
        filter = window_filter(sc_window)
        config = stream_configuration(filter, max_width=WINDOW_FRAME_WIDTH, fps=STREAM_FPS)
        streamed = _Streamed(target=target, window_id=window.id)
        streamed.stream = FrameStream(
            filter,
            config,
            on_frame=lambda frame, t=target: self.create_task(self._on_stream_frame(t, frame)),
            on_stop=lambda error, t=target: self.create_task(self._on_stream_stopped(t, error)),
        )
        self._streamed[target] = streamed
        await streamed.stream.start()
        streamed.last_frame_at = time.monotonic()
        logger.info(f"{self}: streaming {self._label_for(window)} at {config.width()}x{config.height()}")
        if self._monitor is None:
            self._monitor = self.create_task(self._run_monitor(), name="stale-monitor")

    async def remove_target(self, target: str):
        await super().remove_target(target)
        streamed = self._streamed.pop(target, None)
        if streamed and streamed.stream:
            await streamed.stream.stop()
            logger.info(f"{self}: stopped streaming {target}")
        self._filters.pop(target, None)
        if not self._streamed and self._monitor:
            monitor, self._monitor = self._monitor, None
            await self.cancel_task(monitor)

    async def cleanup(self):
        for target in list(self._streamed):
            await self.remove_target(target)
        await super().cleanup()

    #
    # Captures
    #

    async def capture(self, target: str):
        # The cadence. Asking for the screen is asking for the moment: a still
        # of the display, then one of every content window, taken together
        # so they share the moment. Streamed windows push their own frames.
        if target in self._streamed:
            return
        if target == SCREEN_TARGET:
            await self._tick()
            return
        await self._still(target)

    async def _tick(self):
        moment = int(time.time())
        await self._still(SCREEN_TARGET, moment=moment)
        own = self._registry.own_pid
        windows = [w for w in self._registry.windows if w.pid != own]
        targets = [f"{WINDOW_PREFIX}{w.id}" for w in windows if f"{WINDOW_PREFIX}{w.id}" not in self._streamed]
        if targets:
            t0 = time.monotonic()
            await asyncio.gather(*(self._still(t, moment=moment, skip_blank=True) for t in targets))
            logger.trace(f"{self}: {len(targets)} window stills in {(time.monotonic() - t0) * 1000:.0f} ms")

    async def capture_now(self, target: str):
        # A look wants the picture as it is right now, watched or not.
        await self._still(target)

    async def _still(self, target: str, *, moment: Optional[int] = None, skip_blank: bool = False):
        if target not in self._targets and not self._window_for(target):
            logger.warning(f"{self}: unknown target {target!r}")
            return
        if target in self._busy:
            return

        self._busy.add(target)
        try:
            filter, config = self._filter_for(target)
            t0 = time.monotonic()
            image = await take_still(filter, config)
            logger.trace(f"{self}: still of {target} in {(time.monotonic() - t0) * 1000:.0f} ms")
        except Exception as e:  # noqa: BLE001 - a failed still is not fatal
            if target in self._failing:
                logger.debug(f"{self}: capture of {target} still failing: {e}")
            else:
                logger.warning(f"{self}: capture of {target} failed: {e}")
                self._failing.add(target)
            # The display or window may have changed; rebuild the filter next time.
            self._filters.pop(target, None)
            return
        finally:
            self._busy.discard(target)

        if target in self._failing:
            self._failing.discard(target)
            logger.info(f"{self}: capture of {target} is back")

        if skip_blank and blank_fraction(image) >= BLANK_FRACTION:
            # A hidden tab, or an occlusion-aware app on another Space: the
            # capture worked, the app did not paint. Nothing to remember.
            logger.trace(f"{self}: {target} is blank, skipped")
            return

        await self._push(target, image, moment=moment)

    async def _push(self, target: str, image, *, moment: Optional[int] = None):
        if target == SCREEN_TARGET:
            app, window = self._registry.frontmost()
            frame = ScreenFrame(
                target=target,
                image=image,
                timestamp=int(time.time()),
                app=app.name if app else None,
                title=window.title if window else None,
                role="screen",
                moment=moment,
            )
        else:
            window = self._window_for(target)
            frame = ScreenFrame(
                target=target,
                image=image,
                timestamp=int(time.time()),
                app=window.app if window else None,
                title=window.title if window else None,
                role="window",
                moment=moment,
                rect=tuple(int(v) for v in window.frame) if window else None,
            )
        await self.push_frame(frame)

    def _filter_for(self, target: str) -> tuple:
        cached = self._filters.get(target)
        if cached:
            return cached
        if target == SCREEN_TARGET:
            displays = self._registry.displays
            if not displays:
                raise RuntimeError("no display")
            filter = display_filter(displays[0])
            config = stream_configuration(filter, max_width=self._width)
            logger.debug(f"{self}: {target} is display {displays[0].displayID()} at {config.width()}x{config.height()}")
        else:
            window = self._window_for(target)
            sc_window = self._registry.sc_window(window.id) if window else None
            if sc_window is None:
                raise RuntimeError(f"{target} is not on screen any more")
            filter = window_filter(sc_window)
            config = stream_configuration(filter, max_width=WINDOW_FRAME_WIDTH)
        self._filters[target] = (filter, config)
        return filter, config

    #
    # Streams and staleness
    #

    async def _on_stream_frame(self, target: str, frame: StreamFrame):
        streamed = self._streamed.get(target)
        if streamed is None:
            return
        now = time.monotonic()

        if frame.status == SCK.SCFrameStatusSuspended:
            await self._set_stale(streamed, "it has been minimized or hidden")
            return
        if frame.image is None:
            return

        streamed.last_frame_at = now
        if blank_fraction(frame.image) >= BLANK_FRACTION:
            streamed.blank_since = streamed.blank_since or now
            if now - streamed.blank_since >= BLANK_AFTER_SECS:
                await self._set_stale(
                    streamed, "it looks blank; if it is on another Space the app may have stopped drawing"
                )
        else:
            streamed.blank_since = None
            await self._set_fresh(streamed)

        await self._push(target, frame.image)

    async def _on_stream_stopped(self, target: str, error: Optional[str]):
        streamed = self._streamed.get(target)
        if streamed is None:
            return
        logger.warning(f"{self}: stream of {target} stopped: {error}")
        await self._set_stale(streamed, "its stream stopped")

    async def _run_monitor(self):
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for streamed in list(self._streamed.values()):
                if (
                    streamed.stale_reason is None
                    and streamed.last_frame_at is not None
                    and now - streamed.last_frame_at > STALE_AFTER_SECS
                ):
                    await self._set_stale(streamed, "no frames are coming from it; it may be minimized or hidden")

    async def _set_stale(self, streamed: _Streamed, reason: str):
        if streamed.stale_reason is not None:
            return
        streamed.stale_reason = reason
        logger.info(f"{self}: {streamed.target} is stale: {reason}")
        await self._call_event_handler("on_target_stale", streamed.target, reason)

    async def _set_fresh(self, streamed: _Streamed):
        if streamed.stale_reason is None:
            return
        streamed.stale_reason = None
        logger.info(f"{self}: {streamed.target} is fresh again")
        await self._call_event_handler("on_target_fresh", streamed.target)

    def _on_registry_event(self, event: RegistryEvent):
        if event.kind != EventKind.CLOSED:
            return
        target = self._target_for(event.window)
        if target in self._streamed:
            self.create_task(self._lose(target, "the window was closed"))

    async def _lose(self, target: str, reason: str):
        await self.remove_target(target)
        await self._call_event_handler("on_target_lost", target, reason)

    #
    # Registry helpers
    #

    @staticmethod
    def _target_for(window: Window) -> str:
        return f"{WINDOW_PREFIX}{window.id}"

    def _window_for(self, target: str) -> Optional[Window]:
        if not target.startswith(WINDOW_PREFIX):
            return None
        window_id = int(target[len(WINDOW_PREFIX) :])
        return next((w for w in self._registry.windows if w.id == window_id), None)

    def _front_window_of(self, pid: int) -> Optional[Window]:
        windows = [w for w in self._registry.windows if w.pid == pid]
        windows.sort(key=lambda w: (not w.on_screen, -(w.frame[2] * w.frame[3])))
        return windows[0] if windows else None

    @staticmethod
    def _label_for(window: Window) -> str:
        return f"the {window.app} window"
