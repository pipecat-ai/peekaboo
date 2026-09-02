#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import io
import os
import time
from typing import Optional

from loguru import logger
from pipecat.bus.messages import BusJobRequestMessage
from pipecat.pipeline.job_context import JobStatus
from pipecat.pipeline.job_decorator import job
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.services.anthropic.llm import AnthropicLLMService

from links import find_join_url
from processors.frames import ScreenFrame, WatchFrame
from processors.gate import ChangeGate
from processors.screen_bridge import SCREEN_BRIDGE
from processors.vision import VisionImageContextProcessor, VisionImageProcessor
from sources.base import CAPTURE_INTERVAL_SECS, BaseFrameSource
from sources.transport import TransportScreenSource
from store.models import Observation
from store.sqlite_store import SQLiteStore
from workers.names import SCREEN_WORKER

# How long a `frame` job waits for the source to deliver.
FRAME_TIMEOUT_SECS = 8.0

# Always on the watchlist, as item 0: the reminders the OS and other apps put
# on screen. A hit becomes a meeting moment for whoever subscribed.
NOTIFICATION_WATCH = (
    "A notification, banner, alert, or popup about a meeting, call, or scheduled "
    "event that is starting soon or now, from a calendar app, Zoom, Meet, Teams, "
    "Slack, or similar. Report its title, its time, and any visible join link."
)

# The same notification is not announced again within this window.
NOTIFICATION_DEDUP_SECS = 10 * 60

FRAME_JPEG_QUALITY = 80

IMAGE_SYSTEM_INSTRUCTION = """

You are a vision agent. You will be given images and must analyze them.

Your output must be a JSON object with the following fields:

FIELDS

1. "type"
   Specifies the kind of output. Must be one of:
     - "description": The image does not contain anything relevant to the watchlist.
     - "watchlist": The image contains something relevant to the watchlist.

2. "content"
   A concise description of the image or a direct response to the user’s query.
   Your responses are spoken aloud. Avoid emojis, bullet points, or any symbols
   that are difficult to vocalize. Speak naturally, clearly, and in full sentences
   that are easy to understand.

3. "timestamp"
   The Unix timestamp (in seconds) when the image was received.

4. "watchlist" (optional)
   Include this field only when "type" is "watchlist".
   Its value must be a list of watchlist item numbers detected in the image.

5. "verbatim_text"
   A list of short strings copied exactly as they appear on screen: window
   titles, URLs, error messages, commands, numbers, names, and anything a
   person might later search for. Up to twelve items, most important first.
   Copy them character for character. Leave the list empty if there is no
   legible text.

WATCHLIST RULES

- When "type" is "watchlist", the "content" must be extremely brief.

WATCHLIST ITEMS:
"""

IMAGE_OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "content": {"type": "string"},
            "watchlist": {"type": "array"},
            "timestamp": {"type": "integer"},
            "verbatim_text": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["type", "content", "timestamp", "verbatim_text"],
        "additionalProperties": False,
    },
}


class ScreenWorker(PipelineWorker):
    """Watches the screen: captures frames, describes changes, keeps the record.

    The pipeline is a frame source, the change gate, and the image branch.
    Where frames come from is the source's business: the OS on a Mac, or the
    transport's screen share when running against a browser. Only a
    transport-backed source needs this worker bridged onto the bus, since
    that is how its frames arrive.

    Every changed frame is described, stored with its picture and verbatim
    text, and checked against the watchlist. This worker never sees a
    question; the vision worker asks it for a picture instead.

    Meeting and schedule notifications that other apps put on screen are
    always watched for, from any app: that is what a screen assistant sees
    instead of reading a calendar. A hit goes to every subscriber as a
    meeting moment, with the join link when one is visible.

    Jobs:

    - ``capture``: ``{"action": "start" | "stop"}`` the capture cadence.
    - ``watch``: ``{"query": ...}`` adds a watchlist item. Stays open; every
      hit is a ``{"say": text}`` update.
    - ``subscribe``: stays open; on-screen reminders arrive on it as urgent
      ``{"moment": {...}}`` updates.
    - ``frame``: ``{"target": ..., "fresh": bool}`` answers with a picture as
      JPEG bytes: the next capture when fresh, else the latest one seen.
    """

    def __init__(
        self,
        *,
        store: SQLiteStore,
        source: Optional[BaseFrameSource] = None,
        **kwargs,
    ):
        self._store = store
        self._frame_source = source or TransportScreenSource()
        self._gate = ChangeGate()
        self._image_processor = VisionImageProcessor(
            system_instruction=IMAGE_SYSTEM_INSTRUCTION, watchlist=[NOTIFICATION_WATCH]
        )
        self._context_processor = VisionImageContextProcessor()

        # Watch job ids, in watchlist order after the built-in notification
        # item: index N is watchlist item N + 1.
        self._watch_jobs: list[str] = []
        # Who wants on-screen reminders, and which were announced recently.
        self._subscribers: list[str] = []
        self._announced: dict[str, float] = {}
        # The latest frame seen per target, and who is waiting for the next one.
        self._latest: dict[str, ScreenFrame] = {}
        self._waiting: dict[str, list[asyncio.Future]] = {}

        pipeline = self._build_pipeline()

        # Frames that come through a transport arrive over the bus from the
        # voice worker's screen bridge. Frames from the OS need no bridge.
        bridged = (
            (SCREEN_BRIDGE,) if isinstance(self._frame_source, TransportScreenSource) else None
        )

        super().__init__(
            pipeline,
            name=SCREEN_WORKER,
            bridged=bridged,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            # No transport here, so no speaking frames: never idle out.
            idle_timeout_secs=None,
            **kwargs,
        )

        self._gate.add_event_handler("on_screen_frame", self._on_screen_frame)
        self._image_processor.add_event_handler("on_watchlist_hit", self._on_watchlist_hit)
        self._context_processor.add_event_handler("on_image_analysis", self._on_image_analysis)
        self._context_processor.add_event_handler(
            "on_analysis_finished", self._on_analysis_finished
        )

    def _build_pipeline(self) -> Pipeline:
        llm = AnthropicLLMService(
            name="ScreenAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            # A request that hangs on connect is retried once.
            retry_on_timeout=True,
            settings=AnthropicLLMService.Settings(
                extra={
                    "extra_headers": {
                        "anthropic-beta": "structured-outputs-2025-11-13",
                    },
                    "extra_body": {
                        "output_format": IMAGE_OUTPUT_FORMAT,
                    },
                },
            ),
        )

        aggregators = LLMContextAggregatorPair(LLMContext())

        return Pipeline(
            [
                self._frame_source,  # Where screen frames come from
                self._gate,  # Marks each frame changed or not
                self._image_processor,  # Changed frames go to the model
                aggregators.user(),
                llm,
                aggregators.assistant(),
                self._context_processor,  # The model's JSON becomes events
            ]
        )

    @property
    def targets(self) -> set[str]:
        return self._frame_source.targets

    #
    # Jobs
    #

    @job(name="capture")
    async def _capture(self, message: BusJobRequestMessage):
        action = str((message.payload or {}).get("action", "start"))
        if action == "start":
            await self._frame_source.start(CAPTURE_INTERVAL_SECS)
        else:
            await self._frame_source.stop()
        await self.send_job_response(message.job_id, {"capturing": self._frame_source.capturing})

    @job(name="watch")
    async def _watch(self, message: BusJobRequestMessage):
        query = str((message.payload or {}).get("query", ""))

        logger.debug(f"{self}: watch: {query}")

        # Stays open. Hits arrive as urgent updates on this job.
        self._watch_jobs.append(message.job_id)
        await self.queue_frame(WatchFrame(query=query))

        # What the user is waiting for may already be on screen: let the next
        # frame through the gate even if nothing has changed, and get one now.
        self._gate.reset()
        for target in self._frame_source.targets:
            await self._frame_source.capture_now(target)

    @job(name="subscribe")
    async def _subscribe(self, message: BusJobRequestMessage):
        logger.debug(f"{self}: {message.source} subscribed to on-screen reminders")
        self._subscribers.append(message.job_id)

    @job(name="frame")
    async def _frame(self, message: BusJobRequestMessage):
        payload = message.payload or {}
        target = str(payload.get("target") or next(iter(self._frame_source.targets), "screen"))
        fresh = bool(payload.get("fresh", True))

        frame = None if fresh else self._latest.get(target)
        if frame is None:
            frame = await self._next_frame(target)

        if frame is None:
            await self.send_job_response(
                message.job_id,
                {"error": f"no frame of {target} within {FRAME_TIMEOUT_SECS}s"},
                status=JobStatus.ERROR,
                urgent=True,
            )
            return

        image = frame.image
        data = await asyncio.to_thread(_encode_jpeg, image)

        logger.debug(f"{self}: frame of {target} for {message.source}: {frame}")

        await self.send_job_response(
            message.job_id,
            {
                "target": target,
                "timestamp": frame.timestamp,
                "key": frame.key,
                "format": "image/jpeg",
                "size": list(image.size),
                "image": data,
            },
            urgent=True,
        )

    async def _next_frame(self, target: str) -> Optional[ScreenFrame]:
        """Capture now and wait for the frame to come through the gate."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting.setdefault(target, []).append(future)
        try:
            await self._frame_source.capture_now(target)
            return await asyncio.wait_for(future, timeout=FRAME_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            logger.warning(f"{self}: no frame of {target} within {FRAME_TIMEOUT_SECS}s")
            return None
        finally:
            waiting = self._waiting.get(target, [])
            if future in waiting:
                waiting.remove(future)

    #
    # Pipeline events
    #

    async def _on_screen_frame(self, gate, frame: ScreenFrame):
        self._latest[frame.target] = frame
        for future in self._waiting.pop(frame.target, []):
            if not future.done():
                future.set_result(frame)

    async def _on_analysis_finished(self, processor, ok: bool):
        self._image_processor.set_idle()
        if not ok:
            # The frame was marked seen but never described: let the next one
            # through even if the screen hasn't moved.
            self._gate.reset()

    async def _on_image_analysis(self, processor, data: dict):
        # The analysis belongs to the frame the image branch just sent. Store
        # them together: the description and text for search, the frame so
        # the memory can be shown later.
        sent = self._image_processor.take_last_sent()

        kind = data.get("type")
        verbatim = [str(item) for item in data.get("verbatim_text") or [] if str(item).strip()]

        observation = Observation(
            timestamp=sent.timestamp if sent else int(data.get("timestamp") or time.time()),
            target=sent.target if sent else "screen",
            kind="watchlist" if kind == "watchlist" else "description",
            content=str(data.get("content", "")),
            verbatim_text=verbatim,
            frame_hash=sent.key if sent else None,
            app=sent.app if sent else None,
            title=sent.title if sent else None,
        )

        if sent and sent.key:
            shot, thumb = await self._store.save_frame(sent.image, sent.timestamp, sent.key)
            observation.screenshot_path = shot
            observation.thumbnail_path = thumb

        await self._store.add(observation)

    async def _on_watchlist_hit(self, processor, content: dict):
        text = str(content.get("content", ""))
        for item in content.get("watchlist") or []:
            try:
                index = int(item)
            except (TypeError, ValueError):
                continue
            if index == 0:
                await self._on_notification(text, content.get("verbatim_text") or [])
                continue
            index -= 1
            if 0 <= index < len(self._watch_jobs) and text:
                await self.send_job_update(self._watch_jobs[index], {"say": text}, urgent=True)

    async def _on_notification(self, text: str, verbatim: list):
        if not text or not self._subscribers:
            return

        # The same banner seen again, or still on screen, is one reminder.
        key = " ".join(text.lower().split())[:80]
        now = time.monotonic()
        self._announced = {
            k: t for k, t in self._announced.items() if now - t < NOTIFICATION_DEDUP_SECS
        }
        if key in self._announced:
            return
        self._announced[key] = now

        moment = {
            "kind": "meeting",
            "source": "screen",
            "text": text,
            "verbatim_text": [str(v) for v in verbatim],
            "join_url": find_join_url(*[str(v) for v in verbatim], text),
        }
        logger.info(f"{self}: on-screen reminder: {text}")
        for job_id in list(self._subscribers):
            await self.send_job_update(job_id, {"moment": moment}, urgent=True)


def _encode_jpeg(image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=FRAME_JPEG_QUALITY)
    return buffer.getvalue()
