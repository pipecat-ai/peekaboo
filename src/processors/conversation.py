#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from collections.abc import Callable
from typing import Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class ConversationState(FrameProcessor):
    """Knows whether anyone is talking.

    Sits right after the LLM, where it sees the user's turn frames coming
    down, the bot's speaking frames coming up from the transport, and the
    LLM's own response boundaries. Passes everything through untouched.
    """

    def __init__(self, *, on_change: Optional[Callable[[str], None]] = None, **kwargs):
        super().__init__(**kwargs)
        self.user_speaking = False
        self.bot_speaking = False
        self.llm_busy = False
        self.calling_tools = False
        # Told the spoken-form state whenever it changes: idle, listening,
        # thinking, speaking. The menu bar shows it.
        self._on_change = on_change
        self._last_state = self.state

    @property
    def idle(self) -> bool:
        return not (self.user_speaking or self.bot_speaking or self.llm_busy or self.calling_tools)

    @property
    def state(self) -> str:
        if self.user_speaking:
            return "listening"
        if self.bot_speaking:
            return "speaking"
        if self.llm_busy or self.calling_tools:
            return "thinking"
        return "idle"

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            self.user_speaking = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
        elif isinstance(frame, LLMFullResponseStartFrame):
            self.llm_busy = True
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.llm_busy = False
        elif isinstance(frame, FunctionCallsStartedFrame):
            self.calling_tools = True
        elif isinstance(frame, FunctionCallResultFrame):
            self.calling_tools = False
        elif isinstance(frame, InterruptionFrame):
            self.llm_busy = False
            self.calling_tools = False

        if self._on_change and self.state != self._last_state:
            self._last_state = self.state
            self._on_change(self._last_state)

        await self.push_frame(frame, direction)
