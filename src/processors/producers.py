#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from typing import Optional

from pipecat.frames.frames import Frame, UserImageRawFrame
from pipecat.processors.producer_processor import ProducerProcessor
from processors.frames import (
    VisionQueryFrame,
    VisionRequestFrame,
    VisionResponseFrame,
    VoiceAgentStartedFrame,
    VoiceAgentStoppedFrame,
)


class VoiceProducer(ProducerProcessor):
    def __init__(self):
        super().__init__(filter=self._filter_frames)
        self._query_frame: Optional[VisionQueryFrame] = None

    async def _filter_frames(self, frame: Frame) -> bool:
        return isinstance(
            frame,
            (UserImageRawFrame, VoiceAgentStartedFrame, VoiceAgentStoppedFrame, VisionQueryFrame),
        )


class VisionProducer(ProducerProcessor):
    def __init__(self):
        super().__init__(filter=self._filter_frames)

    async def _filter_frames(self, frame: Frame) -> bool:
        return isinstance(frame, (VisionResponseFrame, VisionRequestFrame))
