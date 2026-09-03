#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
from collections import deque
import io
import json
import os
import time
from dataclasses import dataclass
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
from processors.frames import ScreenFrame
from processors.gate import ChangeGate
from processors.screen_bridge import SCREEN_BRIDGE
from processors.vision import VisionImageContextProcessor, VisionImageProcessor, WatchItem
from sources.base import CAPTURE_INTERVAL_SECS, BaseFrameSource
from sources.transport import TransportScreenSource
from store.models import Observation
from store.sqlite_store import SQLiteStore
from workers.names import SCREEN_WORKER

# How long a `frame` job waits for the source to deliver.
FRAME_TIMEOUT_SECS = 8.0
# A watched window has to stay out of sight this long before it is mentioned.
STALE_ANNOUNCE_SECS = 15.0

# Always on the watchlist, as item 0: the reminders the OS and other apps put
# on screen. It looks at notification banners (their own window, cropped) and
# at the screen when that is what is being analysed, never at ordinary window
# content: an email or a chat about an event is not a reminder. A hit becomes
# a meeting moment for whoever subscribed.
NOTIFICATION_WATCH_ID = 0
NOTIFICATION_WATCH = WatchItem(
    id=NOTIFICATION_WATCH_ID,
    query=(
        "A notification banner, alert, or popup about a meeting, call, or scheduled "
        "event that is starting soon or now, from a calendar app, Zoom, Meet, Teams, "
        "Slack, or similar. Report its title, its time, and any visible join link. "
        "Not deliveries, orders, shipping, news, and not an email or a chat message "
        "that merely mentions an event: only a reminder that has popped up."
    ),
    target=("banner", "screen"),
)


@dataclass
class Watcher:
    """One thing someone asked to be told about, on one target."""

    id: int
    job_id: str
    target: str
    label: str
    query: str
    wanted: str = ""
    """What the user asked to watch, in their words; re-resolved on restore."""
    enabled: bool = True
    restored: bool = False
    """Recreated at startup: its hits go to the subscriber, not to a watch job."""

    def describe(self) -> dict:
        return {"id": self.id, "target": self.label, "condition": self.query, "enabled": self.enabled}

    def saved(self) -> dict:
        return {"wanted": self.wanted, "condition": self.query, "enabled": self.enabled}


WATCHERS_FILE = "watchers.json"

# The same notification is not announced again within this window.
NOTIFICATION_DEDUP_SECS = 6 * 60 * 60
ANNOUNCED_FILE = "announced.json"

FRAME_JPEG_QUALITY = 80

# Runs on every changed frame, all day: the cheapest capable tier (plan §5).
SCREEN_MODEL = "claude-haiku-4-5"

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

4. "watchlist"
   The list of watchlist item numbers detected in the image. Empty when
   "type" is "description"; never empty when "type" is "watchlist".

5. "verbatim_text"
   A list of short strings copied exactly as they appear on screen: window
   titles, URLs, error messages, commands, numbers, names, and anything a
   person might later search for. Up to twelve items, most important first.
   Copy them character for character. Leave the list empty if there is no
   legible text.

WATCHLIST RULES

- When "type" is "watchlist", the "content" must be extremely brief.
- The query may carry "previous": what this same window showed the last
  time it was described. Items about a change or an event ("I get new
  messages", "the build finishes", "someone replies") are judged by
  comparing the image with "previous": report them when the image shows
  something that was not there before, and not otherwise. Without
  "previous", judge from the image alone.
- When two images are given, the second is the part of the window that
  changed since the previous frame, enlarged. Read small changes from it: a
  name turning bold, a badge or count appearing, a new line.
- "window_title_before" means the frame was taken because the title changed;
  compare it with "window_title": counts like "3 new items" are unread
  messages, and a new count is new messages.
- In messaging apps (Slack, Discord, Messages, Mail), the "content" names the
  conversations shown as unread: bold channel or person names, unread
  counts, dots. Put those names in "verbatim_text" too, so the next
  comparison has them.

WATCHLIST ITEMS:
"""

# Strict on purpose: the watchlist ids route hits to watchers, so they are
# required (empty for a plain description) and integers. The schema validator
# accepts no length caps, so the token limit below is what bounds a runaway.
IMAGE_OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["description", "watchlist"]},
            "content": {"type": "string"},
            "watchlist": {"type": "array", "items": {"type": "integer"}},
            "timestamp": {"type": "integer"},
            "verbatim_text": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["type", "content", "watchlist", "timestamp", "verbatim_text"],
        "additionalProperties": False,
    },
}

# A description is a paragraph and a dozen strings; anything longer is the
# model looping, and the sooner it is cut off the sooner the frame is retried.
SCREEN_MAX_TOKENS = 1024
# Cost telemetry: what one image analysis costs, roughly, and how often to log the rate.
ANALYSIS_COST_USD = 0.005
ANALYSIS_LOG_EVERY = 20


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

    Targets are what the user said ("the terminal", "Chrome", or nothing for
    the whole screen); the source resolves them. A watch on a window starts
    that window's stream, and its frames are checked only against the
    watchers bound to it.

    Jobs:

    - ``capture``: ``{"action": "start" | "stop"}`` the capture cadence.
    - ``watch``: ``{"query": ..., "target": ...}`` adds a watcher. Stays open;
      every hit is a ``{"say": text}`` update, and the target going stale,
      fresh, or away is a ``{"warning": text}`` update. Closed when the
      watcher is removed or its window closes.
    - ``unwatch``: ``{"id": int}`` or ``{"target": ...}``; nothing removes
      every watcher. Answers with the watchers removed.
    - ``enable_watcher``: ``{"id": int, "enabled": bool}`` pauses or resumes a
      watcher without forgetting it; id 0 is the built-in reminders watch.
    - ``list_watchers``: answers with the watchers and the built-in's state.
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
        self._analyses: deque[float] = deque()
        self._stale_timers: dict[str, asyncio.Task] = {}
        self._stale_announced: set[str] = set()
        self._analyses_total = 0
        self._image_processor = VisionImageProcessor(
            system_instruction=IMAGE_SYSTEM_INSTRUCTION, watchlist=[NOTIFICATION_WATCH]
        )
        self._context_processor = VisionImageContextProcessor()

        # Watchers by id; the id is also the watchlist item number the model
        # reports, so ids are never reused.
        self._watchers: dict[int, Watcher] = {}
        self._next_watcher_id = NOTIFICATION_WATCH_ID + 1
        self._notifications_enabled = True
        self._restored = False
        # Who wants on-screen reminders, and which were announced recently.
        self._subscribers: list[str] = []
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
        self._frame_source.add_event_handler("on_target_stale", self._on_target_stale)
        self._frame_source.add_event_handler("on_target_fresh", self._on_target_fresh)
        self._frame_source.add_event_handler("on_target_lost", self._on_target_lost)

    def _build_pipeline(self) -> Pipeline:
        llm = AnthropicLLMService(
            name="ScreenAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            # A request that hangs on connect is retried once.
            retry_on_timeout=True,
            settings=AnthropicLLMService.Settings(
                model=SCREEN_MODEL,
                max_tokens=SCREEN_MAX_TOKENS,
                extra={
                    # Structured outputs are GA: output_config.format, no beta header.
                    "extra_body": {"output_config": {"format": IMAGE_OUTPUT_FORMAT}},
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
        payload = message.payload or {}
        query = str(payload.get("query", ""))
        wanted = str(payload.get("target") or "")

        try:
            watcher, resolved = await self._add_watcher(query, wanted, message.job_id)
        except Exception as e:  # noqa: BLE001 - reported to the requester
            await self.send_job_response(message.job_id, {"error": str(e)}, status=JobStatus.ERROR)
            return
        self._save()

        # Stays open. Hits and warnings arrive as urgent updates on this job.
        if wanted and not resolved.exact:
            await self.send_job_update(
                message.job_id,
                {"say": f"I couldn't find {wanted}, so I'm watching the whole screen for that."},
                urgent=True,
            )

        # What the user is waiting for may already be on screen: let the next
        # frame through the gate even if nothing has changed, and get one now.
        self._gate.reset(resolved.target)
        await self._frame_source.capture_now(resolved.target)

    async def _add_watcher(self, query: str, wanted: str, job_id: str, *, enabled: bool = True, restored: bool = False):
        resolved = self._frame_source.resolve(wanted)
        logger.debug(f"{self}: watch {resolved.target} ({resolved.label}): {query}")
        try:
            await self._frame_source.add_target(resolved.target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"cannot watch {resolved.label}: {e}") from e
        watcher = Watcher(
            id=self._next_watcher_id,
            job_id=job_id,
            target=resolved.target,
            label=resolved.label,
            query=query,
            wanted=wanted,
            enabled=enabled,
            restored=restored,
        )
        self._next_watcher_id += 1
        self._watchers[watcher.id] = watcher
        if enabled:
            self._image_processor.add_watch(WatchItem(id=watcher.id, query=query, target=resolved.target))
        return watcher, resolved

    #
    # Persistence: the intent behind each watcher, restored at the next launch
    #

    def _save(self):
        path = self._store.root / WATCHERS_FILE
        try:
            path.write_text(json.dumps([w.saved() for w in self._watchers.values()], indent=2))
        except OSError as e:
            logger.warning(f"{self}: could not save watchers: {e}")

    async def _restore(self, job_id: str):
        """Recreate saved watchers, delivering their hits on ``job_id``."""
        if self._restored:
            return
        self._restored = True
        # Pick up where the last session left off: windows still showing the
        # frame described then are not described again, and their last
        # description is the "previous" for watch conditions.
        try:
            known = await self._store.last_frames()
            self._image_processor.seed(known)
            logger.info(f"{self}: seeded {len(known)} window(s) from the store")
        except Exception as e:  # noqa: BLE001 - a cold start is fine
            logger.warning(f"{self}: could not seed from the store: {e}")
        path = self._store.root / WATCHERS_FILE
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        for entry in saved:
            try:
                await self._add_watcher(
                    str(entry.get("condition", "")),
                    str(entry.get("wanted", "")),
                    job_id,
                    enabled=bool(entry.get("enabled", True)),
                    restored=True,
                )
            except Exception as e:  # noqa: BLE001 - the others still come back
                logger.warning(f"{self}: could not restore watcher {entry}: {e}")
        if saved:
            logger.info(f"{self}: restored {len(saved)} watcher(s)")
            self._gate.reset()

    @job(name="unwatch")
    async def _unwatch(self, message: BusJobRequestMessage):
        payload = message.payload or {}
        wanted_id = payload.get("id")
        wanted = str(payload.get("target") or "")

        if wanted_id is not None:
            selected = [w for w in self._watchers.values() if w.id == int(wanted_id)]
        elif wanted:
            resolved = self._frame_source.resolve(wanted)
            selected = [w for w in self._watchers.values() if w.target == resolved.target]
        else:
            selected = list(self._watchers.values())

        removed = []
        for watcher in selected:
            await self._remove_watcher(watcher, reason="unwatched")
            removed.append(watcher.describe())
        self._save()
        await self.send_job_response(message.job_id, {"removed": removed})

    @job(name="list_watchers")
    async def _list_watchers(self, message: BusJobRequestMessage):
        await self.send_job_response(
            message.job_id,
            {
                "watchers": [w.describe() for w in self._watchers.values()],
                "builtin": {"enabled": self._notifications_enabled},
            },
        )

    @job(name="enable_watcher")
    async def _enable_watcher(self, message: BusJobRequestMessage):
        payload = message.payload or {}
        watcher_id = int(payload.get("id", -1))
        enabled = bool(payload.get("enabled", True))
        if watcher_id == NOTIFICATION_WATCH_ID:
            self._notifications_enabled = enabled
            if enabled:
                self._image_processor.add_watch(NOTIFICATION_WATCH)
            else:
                self._image_processor.remove_watch(NOTIFICATION_WATCH_ID)
        else:
            watcher = self._watchers.get(watcher_id)
            if watcher is None:
                await self.send_job_response(message.job_id, {"error": "no such watcher"}, status=JobStatus.ERROR)
                return
            watcher.enabled = enabled
            if enabled:
                self._image_processor.add_watch(WatchItem(id=watcher.id, query=watcher.query, target=watcher.target))
            else:
                self._image_processor.remove_watch(watcher.id)
        logger.debug(f"{self}: watcher {watcher_id} {'enabled' if enabled else 'disabled'}")
        self._save()
        await self.send_job_response(message.job_id, {"id": watcher_id, "enabled": enabled})

    async def _remove_watcher(self, watcher: Watcher, *, reason: str):
        self._watchers.pop(watcher.id, None)
        self._image_processor.remove_watch(watcher.id)
        logger.debug(f"{self}: watcher {watcher.id} on {watcher.label} removed: {reason}")
        # Nobody else watches this target: stop its stream.
        if watcher.target != self._frame_source.default_target and not any(
            w.target == watcher.target for w in self._watchers.values()
        ):
            await self._frame_source.remove_target(watcher.target)
        # A restored watcher shares the subscriber's job, which stays open.
        if not watcher.restored:
            await self.send_job_response(watcher.job_id, {"reason": reason}, status=JobStatus.CANCELLED)

    def _watchers_on(self, target: str) -> list[Watcher]:
        return [w for w in self._watchers.values() if w.target == target and w.enabled]

    @job(name="subscribe")
    async def _subscribe(self, message: BusJobRequestMessage):
        logger.debug(f"{self}: {message.source} subscribed to on-screen reminders")
        self._subscribers.append(message.job_id)
        # The first subscriber also receives the hits of watchers saved from
        # the last run, since their own jobs did not survive it.
        await self._restore(message.job_id)

    @job(name="frame")
    async def _frame(self, message: BusJobRequestMessage):
        payload = message.payload or {}
        resolved = self._frame_source.resolve(str(payload.get("target") or ""))
        target = resolved.target
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
                "label": resolved.label,
                "exact": resolved.exact,
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

    def _count_analysis(self):
        """Cost telemetry: the analysis rate, logged every so often."""
        now = time.monotonic()
        self._analyses.append(now)
        while self._analyses and self._analyses[0] < now - 3600:
            self._analyses.popleft()
        self._analyses_total += 1
        if self._analyses_total % ANALYSIS_LOG_EVERY == 0:
            span = max(60.0, now - self._analyses[0]) if len(self._analyses) > 1 else 60.0
            per_hour = len(self._analyses) * 3600 / span
            logger.info(
                f"{self}: {self._analyses_total} analyses this session, "
                f"{per_hour:.0f}/h over the last {span / 60:.0f} min ≈ ${per_hour * ANALYSIS_COST_USD:.2f}/h"
            )

    async def _on_screen_frame(self, gate, frame: ScreenFrame):
        self._latest[frame.target] = frame
        for future in self._waiting.pop(frame.target, []):
            if not future.done():
                future.set_result(frame)
        if frame.role == "screen" and frame.moment is not None and frame.changed and frame.key and self._frame_source.capturing:
            await self._keep_still(frame)

    async def _keep_still(self, frame: ScreenFrame):
        """The screen still of a moment, stored as it is: the context the
        window frames sit in. Not analysed here; the image processor looks at
        the screen on its own, slower cadence for the screen-wide watchlist."""
        shot, thumb = await self._store.save_frame(frame.image, frame.timestamp, frame.key)
        await self._store.add(
            Observation(
                timestamp=frame.timestamp,
                target=frame.target,
                kind="screen",
                content="",
                app=frame.app,
                title=frame.title,
                frame_hash=frame.key,
                screenshot_path=shot,
                thumbnail_path=thumb,
                moment=frame.moment,
                rect=list(frame.rect) if frame.rect else None,
            )
        )

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
        self._count_analysis()
        if sent:
            self._image_processor.remember(sent.target, str(data.get("content", "")))
        if not self._frame_source.capturing:
            # Paused: watchers still get their hits (handled elsewhere), but
            # nothing is remembered.
            logger.debug(f"{self}: paused, not storing the analysis of {sent.target if sent else '?'}")
            return

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
            moment=sent.moment if sent else None,
            rect=list(sent.rect) if sent and sent.rect else None,
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
                item_id = int(item)
            except (TypeError, ValueError):
                continue
            if item_id == NOTIFICATION_WATCH_ID:
                await self._on_notification(text, content.get("verbatim_text") or [])
                continue
            watcher = self._watchers.get(item_id)
            if watcher and text:
                key = "hit" if watcher.restored else "say"
                await self.send_job_update(watcher.job_id, {key: text}, urgent=True)

    #
    # Target health
    #

    async def _on_target_stale(self, source, target: str, reason: str):
        # Spoken only if it lasts: a window that hides for a few seconds while
        # the user switches around is not worth a sentence.
        self._cancel_stale_timer(target)
        self._stale_timers[target] = self.create_task(self._announce_stale(target, reason))

    async def _announce_stale(self, target: str, reason: str):
        await asyncio.sleep(STALE_ANNOUNCE_SECS)
        self._stale_timers.pop(target, None)
        self._stale_announced.add(target)
        for watcher in self._watchers_on(target):
            await self.send_job_update(
                watcher.job_id,
                {"warning": f"I can't see {watcher.label} any more: {reason}."},
                urgent=True,
            )

    def _cancel_stale_timer(self, target: str):
        timer = self._stale_timers.pop(target, None)
        if timer:
            timer.cancel()

    async def _on_target_fresh(self, source, target: str):
        self._cancel_stale_timer(target)
        if target not in self._stale_announced:
            return
        self._stale_announced.discard(target)
        for watcher in self._watchers_on(target):
            await self.send_job_update(
                watcher.job_id, {"warning": f"I can see {watcher.label} again."}, urgent=True
            )

    async def _on_target_lost(self, source, target: str, reason: str):
        self._cancel_stale_timer(target)
        self._stale_announced.discard(target)
        for watcher in self._watchers_on(target):
            await self.send_job_update(
                watcher.job_id,
                {"warning": f"I've stopped watching {watcher.label}: {reason}."},
                urgent=True,
            )
            await self._remove_watcher(watcher, reason=reason)

    def _load_announced(self) -> dict[str, float]:
        try:
            return {str(k): float(v) for k, v in json.loads((self._store.root / ANNOUNCED_FILE).read_text()).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_announced(self, announced: dict[str, float]):
        try:
            (self._store.root / ANNOUNCED_FILE).write_text(json.dumps(announced))
        except OSError as e:
            logger.warning(f"{self}: could not save announced reminders: {e}")

    async def _on_notification(self, text: str, verbatim: list):
        if not text or not self._subscribers:
            return

        # The same banner seen again, still on screen, or seen before a
        # restart, is one reminder: what was announced is kept on disk.
        key = " ".join(text.lower().split())[:80]
        now = time.time()
        announced = self._load_announced()
        announced = {k: t for k, t in announced.items() if now - t < NOTIFICATION_DEDUP_SECS}
        if key in announced:
            return
        announced[key] = now
        self._save_announced(announced)

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
