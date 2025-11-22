#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class HistoryContextProcessor(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._register_event_handler("on_history_analysis")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self._handle_llm_context_frame(frame)
        else:
            await self.push_frame(frame, direction)

    async def _handle_llm_context_frame(self, frame: LLMContextFrame):
        assistant_message = frame.context.messages[-1]
        print(assistant_message)
