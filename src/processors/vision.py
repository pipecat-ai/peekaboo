#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

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

from processors.frames import QuestionFrame, ScreenFrame, VisionWatchlistFrame

# An analysis that takes longer than this is assumed lost, so the image
# branch accepts frames again.
ANALYSIS_TIMEOUT_SECS = 45.0

# A target is analysed at most this often however fast it changes.
MIN_ANALYSIS_INTERVAL_SECS = 15.0
# A banner is short-lived: read it as soon as it appears.
BANNER_ANALYSIS_INTERVAL_SECS = 3.0
# The changed region is sent enlarged when it is a small part of the frame.
CROP_MAX_FRACTION = 0.4
CROP_MAX_WIDTH = 1200
CROP_MAX_SCALE = 3.0


@dataclass(frozen=True)
class WatchItem:
    """Something to watch for, on one target or on every frame."""

    id: int
    query: str
    target: Optional[Union[str, tuple[str, ...]]] = None
    """The frame target this applies to, or several; None means every target."""

    def applies_to(self, target: str) -> bool:
        if self.target is None:
            return True
        if isinstance(self.target, tuple):
            return target in self.target
        return self.target == target


def watchlist_for(items: Iterable[WatchItem], target: str) -> list[WatchItem]:
    """The items a frame of ``target`` is checked against, in id order."""
    return sorted((i for i in items if i.applies_to(target)), key=lambda i: i.id)


@dataclass
class SentFrame:
    """A frame handed to the image model, kept so the analysis can be stored with it."""

    target: str
    timestamp: int
    image: Image.Image
    key: str
    app: Optional[str] = None
    title: Optional[str] = None
    role: str = "window"
    moment: Optional[int] = None
    rect: Optional[tuple[int, int, int, int]] = None


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
        if frame.windows:
            lines = "\n".join(json.dumps(item) for item in frame.windows)
            text = (
                "Open windows, the latest capture of each (the present state of that window, "
                f"whether or not it shows in the picture):\n{lines}\n\n{text}"
            )
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

    One frame at a time. While the model works on one, changed frames of
    other targets wait, the newest per target, and go when it is idle; a
    newer frame of the same target replaces the waiting one. Unchanged frames
    are skipped outright. The worker calls :meth:`set_idle` when an analysis
    finishes.

    Cost is bounded per target: a window is analysed at most once every
    ``MIN_ANALYSIS_INTERVAL_SECS`` however often it changes (a video, a
    scrolling log). A frame that arrives too early waits and is replaced by
    newer ones. The screen still is never analysed: it is the picture of the
    moment, and the windows on it carry the content; a watch item with no
    target is checked against every window and banner instead.
    """

    def __init__(self, *, system_instruction: str, watchlist: Optional[List[WatchItem]] = None):
        super().__init__()
        self._system_instruction = system_instruction
        self._watchlist: Dict[int, WatchItem] = {item.id: item for item in watchlist or []}
        self._watchlist_messages: List[LLMContextMessage] = []
        self._last_sent: Optional[SentFrame] = None
        self._busy_since: Optional[float] = None
        # Changed frames waiting for the model, newest per target, in arrival order.
        self._pending: Dict[str, ScreenFrame] = {}
        # When each target was last sent, for the per-target interval.
        self._sent_at: Dict[str, float] = {}
        # What each target showed last time it was described, so conditions
        # about change ("new messages", "finished") can be judged.
        self._previous: Dict[str, str] = {}
        # Frame keys already described before this session, per target.
        self._known: Dict[str, str] = {}
        self._flush_task: Optional[asyncio.Task] = None

        # Fired with the analysis dict of a frame that matched watchlist items.
        self._register_event_handler("on_watchlist_hit")

    def add_watch(self, item: WatchItem):
        self._watchlist[item.id] = item

    def remove_watch(self, item_id: int):
        self._watchlist.pop(item_id, None)

    @property
    def watchlist(self) -> List[WatchItem]:
        return list(self._watchlist.values())

    @staticmethod
    def _changed_crop(frame: ScreenFrame) -> Optional[Image.Image]:
        """The part of the frame that changed, cut out and enlarged so small
        things (a name turning bold, a badge, one new line) are legible."""
        box = frame.changed_box
        if not box:
            return None
        left, top, right, bottom = box
        w, h = right - left, bottom - top
        if w < 8 or h < 8 or w * h > CROP_MAX_FRACTION * frame.image.size[0] * frame.image.size[1]:
            return None
        crop = frame.image.crop(box)
        scale = min(CROP_MAX_WIDTH / max(1, w), CROP_MAX_SCALE)
        if scale > 1.0:
            crop = crop.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        logger.debug(f"changed region of {frame.target}: {w}x{h} at ({left},{top}), sent at {crop.size}")
        return crop

    def remember(self, target: str, content: str):
        """Keep a target's latest description for the next analysis."""
        if content:
            self._previous[target] = content

    def seed(self, known: Dict[str, tuple[str, str]]):
        """What the store already holds per target: the last analysed frame's
        key and description. The first frame of a target after a restart is
        skipped when it is that same frame, and the description carries over."""
        for target, (key, content) in known.items():
            if key:
                self._known[target] = key
            if content:
                self._previous[target] = content

    def take_last_sent(self) -> Optional[SentFrame]:
        """The frame most recently sent to the model, once."""
        sent, self._last_sent = self._last_sent, None
        return sent

    def set_idle(self):
        """The current analysis is over; the next waiting frame goes."""
        self._busy_since = None
        self._schedule_flush()

    def _interval_for(self, frame: ScreenFrame) -> float:
        if frame.role == "banner":
            return BANNER_ANALYSIS_INTERVAL_SECS
        return MIN_ANALYSIS_INTERVAL_SECS

    def _due_in(self, frame: ScreenFrame) -> float:
        """Seconds until this target may be analysed again; 0 if now."""
        last = self._sent_at.get(frame.target)
        return 0.0 if last is None else max(0.0, last + self._interval_for(frame) - time.monotonic())

    def _schedule_flush(self):
        if self._pending and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = self.create_task(self._flush())

    async def cleanup(self):
        # A flush waiting for a target's interval would outlive the pipeline.
        if self._flush_task and not self._flush_task.done():
            task, self._flush_task = self._flush_task, None
            await self.cancel_task(task)
        await super().cleanup()

    async def _flush(self):
        """Send the first waiting frame that is due; if none is, sleep until
        the earliest becomes due and try again."""
        while self._pending and not self.busy:
            due = [(self._due_in(f), t) for t, f in self._pending.items()]
            wait, target = min(due)
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            frame = self._pending.pop(target)
            await self._send(frame)
            return

    @property
    def pending(self) -> int:
        return len(self._pending)

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

        if isinstance(frame, ScreenFrame):
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
        if frame.role == "screen":
            # The picture of the moment, kept as it is; its windows are
            # described one by one and notifications come as banners.
            return
        known = self._known.pop(frame.target, None)
        if known is not None and known == frame.key and not frame.priority:
            logger.debug(f"{self}: {frame.target} is the frame described before the restart, skipped")
            return
        if self.busy or (self._due_in(frame) > 0 and not frame.priority):
            # Newest frame per target waits; it goes when the model is idle
            # and the target's interval has passed.
            self._pending[frame.target] = frame
            logger.trace(f"{self}: {frame.target} waits ({len(self._pending)} pending, due in {self._due_in(frame):.0f}s)")
            self._schedule_flush()
            return
        await self._send(frame)

    async def _send(self, frame: ScreenFrame):
        # Only the items bound to this frame's target, plus the ones that
        # apply everywhere, numbered by their stable ids.
        items = watchlist_for(self._watchlist.values(), frame.target)
        watchlist_queries = "\n".join(f"{item.id}. {item.query}" for item in items)
        system_instruction = self._system_instruction + watchlist_queries

        system_message = {
            "role": "system",
            "content": system_instruction,
        }

        query = {
            "text": "Describe the image and check if it contains anything from the watchlist",
            "timestamp": frame.timestamp,
        }
        # The model kept calling Slack "Notion": tell it what window this is.
        if frame.app:
            query["app"] = frame.app
        if frame.title:
            query["window_title"] = frame.title
        if frame.priority and frame.previous_title is not None:
            query["window_title_before"] = frame.previous_title
        previous = self._previous.get(frame.target)
        if previous:
            query["previous"] = previous

        crop = self._changed_crop(frame)
        if crop is not None:
            query["changed_region"] = "the second image is the part of the window that changed since the previous frame, enlarged"
        message = await LLMContext.create_image_message(
            image=frame.image.tobytes(),
            size=frame.image.size,
            format="RGB",
            text=json.dumps(query),
        )
        if crop is not None:
            extra = await LLMContext.create_image_message(
                image=crop.tobytes(), size=crop.size, format="RGB", text="The changed region, enlarged."
            )
            message["content"] = list(message["content"]) + list(extra["content"])

        self._last_sent = SentFrame(
            target=frame.target,
            timestamp=frame.timestamp,
            image=frame.image,
            key=frame.key or "",
            app=frame.app,
            title=frame.title,
            role=frame.role,
            moment=frame.moment,
            rect=frame.rect,
        )
        self._busy_since = time.monotonic()
        self._sent_at[frame.target] = self._busy_since

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
        watchlist = item.get("watchlist") or []

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
