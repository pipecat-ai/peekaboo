#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from processors.frames import ScreenFrame
from store.images import changed_box, changed_fraction, frame_key, signature

# Fraction of the signature that has to move for a frame to count as changed.
# 0.2% of a 128x80 signature is about twenty pixels: a line of text, not a
# cursor.
CHANGED_THRESHOLD = 0.002
# A change touching less of the frame than this is local: worth pointing at.
LOCAL_CHANGE_MAX_FRACTION = 0.35


class ChangeGate(FrameProcessor):
    """Marks each screen frame with its identity and whether the screen moved.

    Every frame goes through; consumers decide what to do with an unchanged
    one. The image branch skips it, since there is nothing new to describe,
    while a pending question still wants the latest picture. "Changed" is
    measured against the last frame that was marked changed for the same
    target, so a screen drifting slowly still trips the gate eventually.
    """

    def __init__(self, *, changed_threshold: float = CHANGED_THRESHOLD, **kwargs):
        super().__init__(**kwargs)
        self._changed_threshold = changed_threshold
        self._last: dict[str, bytes] = {}

        # Fired with every screen frame once it is annotated, changed or not,
        # so the worker can hand the latest picture to whoever asked for one.
        self._register_event_handler("on_screen_frame", sync=True)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, ScreenFrame):
            self._annotate(frame)
            await self._call_event_handler("on_screen_frame", frame)

        await self.push_frame(frame, direction)

    def _annotate(self, frame: ScreenFrame):
        sig = signature(frame.image)
        frame.signature = sig
        frame.key = frame_key(sig)

        last = self._last.get(frame.target)
        if last is None:
            frame.changed = True
        else:
            fraction = changed_fraction(sig, last)
            frame.changed = fraction >= self._changed_threshold
            logger.trace(f"{self}: {frame.target} changed {fraction:.4f} -> {frame.changed}")
            if frame.priority and not frame.changed:
                frame.changed = True
            if frame.changed and fraction < LOCAL_CHANGE_MAX_FRACTION:
                frame.changed_box = changed_box(sig, last, frame.image.size)

        if frame.changed:
            self._last[frame.target] = sig

    def reset(self, target: str | None = None):
        """Forget the last frame, so the next one counts as changed."""
        if target is None:
            self._last.clear()
        else:
            self._last.pop(target, None)
