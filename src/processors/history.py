#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.producer_processor import ProducerProcessor
from processors.frames import VisionResponseFrame


class HistoryContextProcessor(FrameProcessor):
    def __init__(self, response_processor: ProducerProcessor):
        super().__init__()
        self._response_processor = response_processor
        self._register_event_handler("on_history_analysis")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self._handle_llm_context_frame(frame)
        else:
            await self.push_frame(frame, direction)

    async def _handle_llm_context_frame(self, frame: LLMContextFrame):
        assistant_message = frame.context.messages[-1]
        await self._response_processor.queue_frame(
            VisionResponseFrame(response=assistant_message["content"])
        )
