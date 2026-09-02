#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
from abc import abstractmethod
from typing import Optional

from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from processors.frames import CaptureRequestFrame

# How often the source asks for a frame of each target while capturing.
CAPTURE_INTERVAL_SECS = 1.0


class BaseFrameSource(FrameProcessor):
    """Head of the vision pipeline: where screen frames come from.

    A source owns a set of targets and emits a :class:`ScreenFrame` for each
    capture. While capturing, it asks for a frame of every target on a fixed
    cadence; a look asks for one right now with :meth:`capture_now`. What a
    capture means is the subclass's business: asking the transport that owns
    the screen share, or reading a window stream on macOS.
    """

    def __init__(self, *, targets: set[str], **kwargs):
        super().__init__(**kwargs)
        self._targets = set(targets)
        self._timer: Optional[asyncio.Task] = None
        self._interval = CAPTURE_INTERVAL_SECS

    @property
    def targets(self) -> set[str]:
        return set(self._targets)

    @property
    def capturing(self) -> bool:
        return self._timer is not None

    async def start(self, interval: float = CAPTURE_INTERVAL_SECS):
        """Start asking for frames on a cadence."""
        if self._timer:
            return
        self._interval = interval
        self._timer = self.create_task(self._run_timer(), name="capture-timer")
        logger.debug(f"{self}: capturing {sorted(self._targets)} every {interval}s")

    async def stop(self):
        """Stop the cadence. Frames already requested may still arrive."""
        if self._timer:
            timer, self._timer = self._timer, None
            await self.cancel_task(timer)
            logger.debug(f"{self}: capture stopped")

    async def capture_now(self, target: str):
        """Ask for one frame of a target right away."""
        await self.capture(target)

    async def cleanup(self):
        await self.stop()
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, CaptureRequestFrame):
            await self.capture(frame.target)
            return

        await self.push_frame(frame, direction)

    async def _run_timer(self):
        while True:
            for target in list(self._targets):
                await self.capture(target)
            await asyncio.sleep(self._interval)

    @abstractmethod
    async def capture(self, target: str):
        """Request one frame of ``target``. The frame arrives later, as a push."""
