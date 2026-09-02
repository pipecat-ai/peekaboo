#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from loguru import logger
from PIL import Image
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMMessagesUpdateFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from processors.frames import QuestionFrame, ScreenFrame, VisionWatchlistFrame, WatchFrame

# An analysis that takes longer than this is assumed lost, so the image
# branch accepts frames again.
ANALYSIS_TIMEOUT_SECS = 45.0


@dataclass
class SentFrame:
    """A frame handed to the image model, kept so the analysis can be stored with it."""

    target: str
    timestamp: int
    image: Image.Image
    key: str
    app: Optional[str] = None
    title: Optional[str] = None


class VisionQueryProcessor(FrameProcessor):
    """Turns a question, its picture, and recent context into one model call.

    A new question interrupts the answer in progress. The interruption is
    pushed from here, so it reaches only this pipeline.
    """

    def __init__(self, *, system_instruction: str):
        super().__init__()
        self._system_instruction = system_instruction

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, QuestionFrame):
            await self._handle_question(frame)
        else:
            await self.push_frame(frame, direction)

    async def _handle_question(self, frame: QuestionFrame):
        await self.push_frame(InterruptionFrame())

        system_message = {"role": "system", "content": self._system_instruction}

        text = frame.query
        if frame.context:
            lines = "\n".join(json.dumps(item) for item in frame.context)
            text = f"Recent screen descriptions, oldest first:\n{lines}\n\nQuestion: {frame.query}"

        if frame.image and frame.size:
            question = await LLMContext.create_image_message(
                format="image/jpeg",
                size=frame.size,
                image=frame.image,
                text=text,
            )
        else:
            question = {"role": "user", "content": text}

        await self.push_frame(LLMMessagesUpdateFrame([system_message, question], run_llm=True))


class VisionImageProcessor(FrameProcessor):
    """Describes changed screen frames and checks them against the watchlist.

    One frame at a time: while the model works on one, others are skipped,
    and unchanged frames are skipped outright. The worker calls
    :meth:`set_idle` when an analysis finishes.
    """

    def __init__(self, *, system_instruction: str, watchlist: Optional[List[str]] = None):
        super().__init__()
        self._system_instruction = system_instruction
        self._watchlist_queries: List[str] = list(watchlist or [])
        self._watchlist_messages: List[LLMContextMessage] = []
        self._last_sent: Optional[SentFrame] = None
        self._busy_since: Optional[float] = None

        # Fired with the analysis dict of a frame that matched watchlist items.
        self._register_event_handler("on_watchlist_hit")

    def take_last_sent(self) -> Optional[SentFrame]:
        """The frame most recently sent to the model, once."""
        sent, self._last_sent = self._last_sent, None
        return sent

    def set_idle(self):
        """The current analysis is over; the next changed frame may go."""
        self._busy_since = None

    @property
    def busy(self) -> bool:
        if self._busy_since is None:
            return False
        if time.monotonic() - self._busy_since > ANALYSIS_TIMEOUT_SECS:
            logger.warning(f"{self}: analysis took too long, accepting frames again")
            self._busy_since = None
            return False
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, WatchFrame):
            self._watchlist_queries.append(frame.query)
        elif isinstance(frame, ScreenFrame):
            await self._handle_screen_frame(frame)
        elif isinstance(frame, VisionWatchlistFrame):
            await self._handle_vision_watchlist_frame(frame)
        else:
            if isinstance(frame, InterruptionFrame):
                self.set_idle()
            await self.push_frame(frame, direction)

    async def _handle_vision_watchlist_frame(self, frame: VisionWatchlistFrame):
        content = frame.content["content"]

        self._watchlist_messages.extend(
            [
                {
                    "role": "user",
                    "content": "Image removed from this message for efficiency.",
                },
                {"role": "assistant", "content": content},
            ]
        )
        await self._call_event_handler("on_watchlist_hit", frame.content)

    async def _handle_screen_frame(self, frame: ScreenFrame):
        if not frame.changed:
            return
        if self.busy:
            logger.trace(f"{self}: busy, skipping {frame}")
            return

        watchlist_queries = "\n".join(f"{i}. {w}" for (i, w) in enumerate(self._watchlist_queries))
        system_instruction = self._system_instruction + watchlist_queries

        system_message = {
            "role": "system",
            "content": system_instruction,
        }

        query = {
            "text": "Describe the image and check if it contains anything from the watchlist",
            "timestamp": frame.timestamp,
        }

        message = await LLMContext.create_image_message(
            image=frame.image.tobytes(),
            size=frame.image.size,
            format="RGB",
            text=json.dumps(query),
        )

        self._last_sent = SentFrame(
            target=frame.target,
            timestamp=frame.timestamp,
            image=frame.image,
            key=frame.key or "",
            app=frame.app,
            title=frame.title,
        )
        self._busy_since = time.monotonic()

        all_messages = [system_message, *self._watchlist_messages, message]

        await self.push_frame(LLMMessagesUpdateFrame(messages=all_messages, run_llm=True))


class VisionImageContextProcessor(FrameProcessor):
    """Turns the image model's JSON into events: an analysis, and watch hits."""

    def __init__(self, *, watchlist_timeout: int = 60):
        super().__init__()
        self._watchlist_timeout = watchlist_timeout
        self._watchlist_timestamps: Dict[int, int] = {}

        self._register_event_handler("on_image_analysis")
        # Fired after every model response with whether it parsed, so the
        # worker can let the next frame through or ask for this one again.
        self._register_event_handler("on_analysis_finished")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self._handle_llm_context_frame(frame)
        else:
            await self.push_frame(frame, direction)

    async def _handle_llm_context_frame(self, frame: LLMContextFrame):
        try:
            assistant_message = frame.context.messages[-1]

            message_content = assistant_message["content"]
            content_dict = json.loads(message_content)

            await self._call_event_handler("on_image_analysis", content_dict)

            # A watchlist match goes out right away.
            if content_dict.get("type", "") == "watchlist":
                await self._maybe_send_watchlist_item(content_dict)

            await self._call_event_handler("on_analysis_finished", True)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            # An interruption can leave half a JSON response behind.
            logger.debug(f"{self}: could not parse analysis: {e}")
            await self._call_event_handler("on_analysis_finished", False)

    async def _maybe_send_watchlist_item(self, item: Mapping[str, Any]):
        timestamp = item["timestamp"]
        watchlist = item["watchlist"]

        # Push watchlist so it can be sent to the voice agent.
        send_watchlist = False
        for w in watchlist:
            diff_time = timestamp - self._watchlist_timestamps.get(w, 0)
            send_watchlist = diff_time >= self._watchlist_timeout
            if send_watchlist:
                self._watchlist_timestamps[w] = timestamp
                break

        if send_watchlist:
            await self.push_frame(VisionWatchlistFrame(content=item), FrameDirection.UPSTREAM)
