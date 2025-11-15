#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    TTSSpeakFrame,
    UserImageRequestFrame,
)
from pipecat.processors.consumer_processor import ConsumerProcessor
from pipecat.processors.frame_processor import FrameDirection
from processors.frames import (
    VisionQueryFrame,
    VisionRequestFrame,
    VisionResponseFrame,
    VoiceAgentStartedFrame,
)
from processors.producers import VisionProducer, VoiceProducer


class VoiceConsumer(ConsumerProcessor):
    def __init__(self, *, producer: VoiceProducer):
        super().__init__(producer=producer)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, VisionQueryFrame):
            await self.push_frame(VisionRequestFrame())
            await self.push_frame(InterruptionFrame())

        await super().process_frame(frame, direction)

        if isinstance(frame, VoiceAgentStartedFrame):
            await self._handle_voice_agent_started_frame(frame)

    async def _handle_voice_agent_started_frame(self, frame: VoiceAgentStartedFrame):
        await self.push_frame(VisionRequestFrame())


class VisionConsumer(ConsumerProcessor):
    def __init__(self, *, producer: VisionProducer):
        super().__init__(producer=producer)
        self._user_id = ""

    def set_user_id(self, user_id: str):
        self._user_id = user_id

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, VisionRequestFrame):
            await self._handle_vision_request_frame(frame)
        elif isinstance(frame, VisionResponseFrame):
            await self._handle_vision_response_frame(frame)

    async def _handle_vision_request_frame(self, frame: VisionRequestFrame):
        await self.push_frame(
            UserImageRequestFrame(
                user_id=self._user_id,
                video_source="screenVideo",
                text=frame.text,
            ),
            FrameDirection.UPSTREAM,
        )

    async def _handle_vision_response_frame(self, frame: VisionResponseFrame):
        await self.push_frame(TTSSpeakFrame(text=frame.response))
