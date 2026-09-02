#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import time

from loguru import logger
from PIL import Image
from pipecat.frames.frames import Frame, UserImageRawFrame
from pipecat.processors.frame_processor import FrameDirection

from processors.frames import ScreenFrame
from processors.screen_bridge import screen_frame_request
from sources.base import BaseFrameSource

SCREEN_TARGET = "screen"

# Frames are scaled to this width before anything looks at them: the model,
# the change gate, the store.
FRAME_WIDTH = 1080


class TransportScreenSource(BaseFrameSource):
    """The shared screen of a Daily or WebRTC session, as one target.

    A capture is a request pushed upstream. It leaves the vision pipeline
    over the bus, reaches the transport that owns the screen share, and the
    frame comes back the same way as a ``UserImageRawFrame``, which this
    source turns into a :class:`ScreenFrame`.
    """

    def __init__(self, **kwargs):
        super().__init__(targets={SCREEN_TARGET}, **kwargs)

    async def capture(self, target: str):
        if target != SCREEN_TARGET:
            logger.warning(f"{self}: unknown target {target!r}")
            return
        await self.push_frame(screen_frame_request(), FrameDirection.UPSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, UserImageRawFrame):
            await super(BaseFrameSource, self).process_frame(frame, direction)
            await self._handle_image(frame)
            return

        await super().process_frame(frame, direction)

    async def _handle_image(self, frame: UserImageRawFrame):
        timestamp = int(time.time())
        image = await self._resize(frame.image, frame.size, frame.format)
        await self.push_frame(ScreenFrame(target=SCREEN_TARGET, image=image, timestamp=timestamp))

    async def _resize(self, data: bytes, size: tuple[int, int], format: str) -> Image.Image:
        def resize() -> Image.Image:
            img = Image.frombytes(format, size, data).convert("RGB")
            if img.width == FRAME_WIDTH:
                return img
            height = max(1, round(img.height * FRAME_WIDTH / img.width))
            return img.resize((FRAME_WIDTH, height), resample=Image.Resampling.LANCZOS)

        return await self.get_event_loop().run_in_executor(None, resize)
