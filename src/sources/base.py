#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
from abc import abstractmethod
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from processors.frames import CaptureRequestFrame

# How often the source asks for a frame of each polled target while capturing.
CAPTURE_INTERVAL_SECS = 1.0


@dataclass(frozen=True)
class Resolved:
    """What a spoken target ("the terminal", "Chrome") turned out to mean."""

    target: str
    """The source's target id."""
    label: str
    """How to refer to it when speaking: "the Ghostty window", "the screen"."""
    exact: bool
    """False when nothing matched and the source fell back to a default."""


class BaseFrameSource(FrameProcessor):
    """Head of the vision pipeline: where screen frames come from.

    A source owns a set of targets and emits a :class:`ScreenFrame` for each
    capture. While capturing, it asks for a frame of every polled target on a
    fixed cadence; a look asks for one right now with :meth:`capture_now`.
    Targets that stream on their own (a watched window on macOS) push frames
    as they arrive and are skipped by the cadence.

    What a target is, and what a capture means, is the subclass's business:
    asking the transport that owns the screen share, or reading the display
    and windows from the OS.

    Events, all with the target id first:

    - ``on_target_stale(target, reason)``: frames stopped, or stopped
      carrying content. ``reason`` is a spoken-form explanation.
    - ``on_target_fresh(target)``: content is back.
    - ``on_target_lost(target, reason)``: the target is gone for good (the
      window closed) and has been removed.
    """

    def __init__(self, *, targets: set[str], **kwargs):
        super().__init__(**kwargs)
        self._targets = set(targets)
        self._timer: Optional[asyncio.Task] = None
        self._interval = CAPTURE_INTERVAL_SECS
        self._register_event_handler("on_target_stale")
        self._register_event_handler("on_target_fresh")
        self._register_event_handler("on_target_lost")

    @property
    def targets(self) -> set[str]:
        return set(self._targets)

    @property
    def default_target(self) -> str:
        return next(iter(sorted(self._targets)))

    @property
    def capturing(self) -> bool:
        return self._timer is not None

    def resolve(self, text: Optional[str]) -> Resolved:
        """Turn what the user said into a target. The default source has one
        target and everything means it."""
        target = self.default_target
        return Resolved(target=target, label=self.label(target), exact=not (text or "").strip())

    def label(self, target: str) -> str:
        return "the screen" if target == self.default_target else target

    async def add_target(self, target: str):
        """Start producing frames of a target that :meth:`resolve` returned."""
        self._targets.add(target)

    async def remove_target(self, target: str):
        if target != self.default_target:
            self._targets.discard(target)

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
        """Request one frame of ``target`` on the cadence. The frame arrives
        later, as a push. Streaming targets may ignore this."""
