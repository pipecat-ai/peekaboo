#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import copy
import json
import time
from typing import Dict, List, Optional, Tuple

from PIL import Image
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMMessagesUpdateFrame,
    UserImageRawFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from processors.frames import (
    VisionQueryFrame,
    VisionRequestFrame,
    VisionResponseFrame,
    VisionWatchlistFrame,
)


class VisionQueryProcessor(FrameProcessor):
    def __init__(self, *, system_instruction: str):
        super().__init__()
        self._system_instruction = system_instruction
        self._image_messages: List[LLMContextMessage] = []
        self._query_frame: Optional[VisionQueryFrame] = None

    async def append_image_messages(self, messages: List[LLMContextMessage]):
        self._image_messages.extend(messages)
        await self._maybe_run_query()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, VisionQueryFrame) and not frame.watchlist:
            await self._handle_vision_query_frame(frame)
        # Ignore UserImageRawFrame they are handled in the other
        # branch. Otherwise, we would try to add it to the context.
        elif not isinstance(frame, UserImageRawFrame):
            await self.push_frame(frame, direction)

    async def _handle_vision_query_frame(self, frame: VisionQueryFrame):
        self._query_frame = frame

    async def _maybe_run_query(self):
        if not self._query_frame:
            return

        image_messages = copy.deepcopy(self._image_messages)
        self._image_messages = []

        system_message = {
            "role": "system",
            "content": self._system_instruction,
        }

        await self.push_frame(
            LLMMessagesUpdateFrame(
                [
                    system_message,
                    *image_messages,
                    {"role": "user", "content": self._query_frame.query},
                ],
                run_llm=True,
            )
        )

        self._query_frame = None


class VisionQueryContextProcessor(FrameProcessor):
    def __init__(self):
        super().__init__()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self._handle_llm_context_frame(frame)
        else:
            await self.push_frame(frame, direction)

    async def _handle_llm_context_frame(self, frame: LLMContextFrame):
        # If we get here we got an assistant message. The content should be our
        # response.
        response = frame.context.messages[-1]["content"]
        await self.queue_frame(VisionResponseFrame(response=response))


class VisionImageProcessor(FrameProcessor):
    def __init__(
        self,
        *,
        system_instruction: str,
        watchlist_timeout: int = 60,
    ):
        super().__init__()
        self._system_instruction = system_instruction
        self._watchlist_timeout = watchlist_timeout
        self._watchlist_queries: List[str] = []
        self._watchlist_messages: List[LLMContextFrame] = []
        self._watchlist_timestamps: Dict[int, int] = {}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, VisionQueryFrame) and frame.watchlist:
            await self._handle_vision_query_frame(frame)
        elif isinstance(frame, UserImageRawFrame):
            await self._handle_user_image_frame(frame)
        elif isinstance(frame, VisionWatchlistFrame):
            await self._handle_vision_watchlist_frame(frame)
        else:
            await self.push_frame(frame, direction)

    async def _handle_vision_watchlist_frame(self, frame: VisionWatchlistFrame):
        content = frame.content["content"]
        timestamp = frame.content["timestamp"]
        watchlist = frame.content["watchlist"]

        # Send the response back to the voice agent if enough time has passed.
        send_response = False
        for w in watchlist:
            diff_time = timestamp - self._watchlist_timestamps.get(w, 0)
            send_response = diff_time >= self._watchlist_timeout
            if send_response:
                self._watchlist_timestamps[w] = timestamp
                break

        if send_response:
            self._watchlist_messages.extend(
                [
                    {
                        "role": "user",
                        "content": "Image removed from this message for efficiency.",
                    },
                    {"role": "assistant", "content": content},
                ]
            )
            await self.push_frame(VisionResponseFrame(response=content))

    async def _handle_vision_query_frame(self, frame: VisionQueryFrame):
        self._watchlist_queries.append(frame.query)

    async def _handle_user_image_frame(self, frame: UserImageRawFrame):
        watchlist_queries = "\n".join(f"{i}. {w}" for (i, w) in enumerate(self._watchlist_queries))
        system_instruction = self._system_instruction + watchlist_queries

        system_message = {
            "role": "system",
            "content": system_instruction,
        }

        text = (
            frame.text or "Describe the image and check if it contains anything from the watchlist"
        )

        query = {
            "text": text,
            "timestamp": int(time.time()),
        }

        image = await self._resize_image(frame.image, frame.size, frame.format)

        message = await LLMContext.create_image_message(
            image=image.tobytes(),
            size=image.size,
            format="RGB",
            text=json.dumps(query),
        )

        all_messages = [system_message, *self._watchlist_messages, message]

        await self.push_frame(LLMMessagesUpdateFrame(messages=all_messages, run_llm=True))

    async def _resize_image(self, data: bytes, size: Tuple[int, int], format: str) -> Image.Image:
        loop = self.get_event_loop()

        def resize() -> Image.Image:
            img = Image.frombytes(format, size, data)

            # Compute new height to maintain aspect ratio
            new_width = 1080
            new_height = int((new_width / img.width) * img.height)

            return img.resize((new_width, new_height), resample=Image.Resampling.LANCZOS)

        return await loop.run_in_executor(None, resize)


class VisionImageContextProcessor(FrameProcessor):
    def __init__(self, *, query_processor: VisionQueryProcessor):
        super().__init__()
        self._query_processor = query_processor

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self._handle_llm_context_frame(frame)
        else:
            await self.push_frame(frame, direction)

    async def _handle_llm_context_frame(self, frame: LLMContextFrame):
        try:
            assistant_message = frame.context.messages[-1]

            # If we get a watchlist response we should send it to the voice agent
            # right away.
            watchlist_content = self._get_watchlist_content(assistant_message)
            if watchlist_content:
                await self.push_frame(
                    VisionWatchlistFrame(content=watchlist_content),
                    FrameDirection.UPSTREAM,
                )
            else:
                await self._send_messages_to_query_processor(assistant_message)
        except Exception:
            # If there's an interruption we might only get half of the JSON
            # response. So, we just ignore that.
            pass

        # We know that every time we get here we can request a new image.
        await self.push_frame(VisionRequestFrame())

    def _get_watchlist_content(self, message: LLMContextMessage) -> Optional[dict]:
        content = message["content"]
        data = json.loads(content)
        if data.get("type", "") == "watchlist":
            return data
        return None

    async def _send_messages_to_query_processor(self, message: LLMContextMessage):
        await self._query_processor.append_image_messages(
            [
                {
                    "role": "user",
                    "content": "Image removed from this message for efficiency.",
                },
                message,
            ]
        )
