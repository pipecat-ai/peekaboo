#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import time
from typing import Optional

from loguru import logger

from macos.capture import display_filter, stream_configuration, take_still
from macos.registry import WindowRegistry
from processors.frames import ScreenFrame
from sources.base import BaseFrameSource

SCREEN_TARGET = "screen"

# The display is captured wider than a window would be, so text stays
# legible after the whole screen is scaled down.
DISPLAY_FRAME_WIDTH = 1280


class ScreenCaptureSource(BaseFrameSource):
    """The Mac's own screen, read from the OS with ScreenCaptureKit.

    One target for now, ``screen``: the main display, as Record mode wants
    (plan §9.10). Every capture is a still from ``SCScreenshotManager``,
    which takes 50 to 200 ms and needs no stream; the base class's cadence
    and ``capture_now`` both land here. Each frame is tagged with the
    frontmost app and window title from the registry, so observations can
    say what the user was looking at.

    Window and app targets, backed by streams so their status is visible,
    come with watchers in M2.
    """

    def __init__(self, *, registry: WindowRegistry, width: int = DISPLAY_FRAME_WIDTH, **kwargs):
        super().__init__(targets={SCREEN_TARGET}, **kwargs)
        self._registry = registry
        self._width = width
        self._filters: dict[str, tuple] = {}
        # Targets with a still in flight. A second request while one is
        # underway is dropped; whoever asked gets the frame on its way.
        self._busy: set[str] = set()
        # Targets whose last capture failed, so a persistent failure (the
        # screen is locked, say) is one warning, not one a second.
        self._failing: set[str] = set()

    async def capture(self, target: str):
        if target not in self._targets:
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
            # The display may have changed; rebuild the filter next time.
            self._filters.pop(target, None)
            return
        finally:
            self._busy.discard(target)

        if target in self._failing:
            self._failing.discard(target)
            logger.info(f"{self}: capture of {target} is back")

        app, window = self._registry.frontmost()
        await self.push_frame(
            ScreenFrame(
                target=target,
                image=image,
                timestamp=int(time.time()),
                app=app.name if app else None,
                title=window.title if window else None,
            )
        )

    def _filter_for(self, target: str) -> tuple:
        cached = self._filters.get(target)
        if cached:
            return cached
        if target != SCREEN_TARGET:
            raise ValueError(f"no filter for target {target!r}")
        displays = self._registry.displays
        if not displays:
            raise RuntimeError("no display")
        filter = display_filter(displays[0])
        config = stream_configuration(filter, max_width=self._width)
        self._filters[target] = (filter, config)
        logger.debug(f"{self}: {target} is display {displays[0].displayID()} at {config.width()}x{config.height()}")
        return filter, config
