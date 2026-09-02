#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from pipecat.frames.frames import (
    Frame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class LLMTurnCollector(FrameProcessor):
    """Collects each LLM response into one string and reports it.

    Placed right after an LLM service. Fires ``on_turn(text, called_tools)``
    when a response ends. ``called_tools`` is True when the response also
    started function calls, which is how a caller tells "let me look that up"
    from a final answer: the LLM service starts its function calls before it
    ends the response, so the flag is set by the time the turn is reported.

    The event is synchronous so the report reaches its handler before the
    next frame moves.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._parts: list[str] = []
        self._called_tools = False
        self._register_event_handler("on_turn", sync=True)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (LLMFullResponseStartFrame, InterruptionFrame)):
            self._parts = []
            self._called_tools = False
        elif isinstance(frame, LLMTextFrame):
            self._parts.append(frame.text)
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._called_tools = True
        elif isinstance(frame, LLMFullResponseEndFrame):
            text = "".join(self._parts).strip()
            called_tools = self._called_tools
            self._parts = []
            self._called_tools = False
            await self._call_event_handler("on_turn", text, called_tools)

        await self.push_frame(frame, direction)
