#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
from datetime import datetime
from typing import Optional

from loguru import logger
from pipecat.frames.frames import InterruptionFrame
from pipecat.bus.messages import (
    BusJobRequestMessage,
    BusJobResponseMessage,
    BusJobResponseUrgentMessage,
    BusJobUpdateMessage,
    BusJobUpdateUrgentMessage,
)
from pipecat.pipeline.job_context import JobError, JobParams, JobStatus
from pipecat.pipeline.job_decorator import job
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.services.llm_service import FunctionCallParams
from pipecat.workers.llm.llm_worker import LLMWorker
from pipecat.workers.llm.tool_decorator import tool

import models
from processors.frames import QuestionFrame
from processors.vision import VisionQueryProcessor
from store.sqlite_store import SQLiteStore
from workers.names import HISTORY_WORKER, SCREEN_WORKER, VISION_WORKER

# How long a look waits for the screen worker to hand over a picture before
# answering from recent descriptions alone.
FRAME_TIMEOUT_SECS = 10.0

# How many recent observations a question gets as context.
CONTEXT_OBSERVATIONS = 8

# A look that has not been answered by then is failed, so a hung model call
# becomes a spoken apology rather than silence. Looks waiting on the history
# worker are exempt; that worker has its own deadline.
LOOK_TIMEOUT_SECS = 30.0

today = datetime.now().astimezone().strftime("%B %d, %Y %Z")

QUERY_SYSTEM_INSTRUCTION = f"""

You answer questions about the user's screen, speaking to them directly
("you have", "your terminal", never "the user"). Today is {today}.

The question usually arrives together with a picture of the screen taken just
now, and with the latest capture of every open window as JSON objects like:

  {{"time": "2026-09-01T09:40", "app": "Google Chrome", "window": "Inbox (12) - Gmail", "content": "...", "verbatim_text": [...]}}

Those windows are the present, whether or not they show in the picture: a
question about mail, a chat, a browser tab, a build, is answered from the
window that holds it even when the picture shows only a terminal. Never say
you cannot see something that is in those captures. Mention when a capture
is more than a few minutes old. Read exact text, numbers, and errors off the
picture or the captures when asked. Recent descriptions of the screen may
also precede the question; use those for what happened moments ago. For
anything earlier, or a past day, call [start_history_agent]; do not guess and
do not say you have no access to the past.

Answer in one or two short sentences unless the user asks for detail; every
extra sentence is seconds of speech. Do not use emojis, bullet points, or
symbols that are difficult to vocalize.

"""


class VisionWorker(LLMWorker):
    """Answers questions about the screen, with the picture in hand.

    A Pipecat ``LLMWorker`` whose pipeline puts a query processor in front
    of the context aggregators: a ``look`` job asks the screen worker for a
    fresh frame, pulls the most recent observations from the store, and the
    processor turns question, picture and context into one model call. When
    the question is about the past, the model's one tool hands it to the
    history worker and this worker forwards what comes back.

    Jobs:

    - ``look``: ``{"query": ..., "target": ...}``, answered once with
      ``{"answer": text}``. ``target`` is what the user called the window or
      app, or empty for the whole screen.
      Text the model writes on its way to the answer is sent as
      ``{"say": text}`` updates.
    """

    def __init__(
        self,
        *,
        store: SQLiteStore,
        screen_worker: str = SCREEN_WORKER,
        history_worker: str = HISTORY_WORKER,
    ):
        self._store = store
        self._screen_worker = screen_worker
        self._history_worker = history_worker

        # The look job whose question is being answered, and its deadline.
        self._current_look: Optional[str] = None
        self._deadline: Optional[asyncio.Task] = None
        # History job id -> the look job waiting on it.
        self._history_to_look: dict[str, str] = {}
        # The window captures each look was given, by job id.
        self._look_sources: dict[str, list[int]] = {}

        llm = models.make_llm(
            models.current().vision,
            name="VisionLLMService",
            # A request that hangs on connect is retried once.
            retry_on_timeout=True,
        )
        self._aggregators = LLMContextAggregatorPair(LLMContext())
        pipeline = Pipeline(
            [
                VisionQueryProcessor(system_instruction=QUERY_SYSTEM_INSTRUCTION),  # Question + picture + context -> one model call
                self._aggregators.user(),
                llm,
                self._aggregators.assistant(),
            ]
        )
        # Active from the start: activation is what sets the tools.
        super().__init__(VISION_WORKER, llm=llm, pipeline=pipeline, active=True)

        @self._aggregators.assistant().event_handler("on_assistant_turn_stopped")
        async def _on_turn_stopped(aggregator, message):
            await self._on_turn(aggregator, message)

    #
    # Jobs
    #

    @job(name="look")
    async def _look(self, message: BusJobRequestMessage):
        payload = message.payload or {}
        query = str(payload.get("query", ""))
        target = str(payload.get("target") or "")

        logger.debug(f"{self}: look: {query} (target: {target or 'screen'})")

        # A new question supersedes one still being answered, unless that one
        # is waiting on the history worker, which will complete it later.
        previous = self._current_look
        if previous and previous not in self._history_to_look.values():
            await self.send_job_response(previous, {}, status=JobStatus.CANCELLED)

        self._current_look = message.job_id

        picture = await self._fresh_frame(target)
        recent = await self._store.recent(limit=CONTEXT_OBSERVATIONS)
        # Every open window's latest capture: the mail, the chat, the browser
        # behind the terminal are part of "now", not of the past. They are the
        # memories this answer draws on, so the window can show them.
        windows = await self._store.latest_windows()
        self._look_sources[message.job_id] = [o.id for o in windows if o.id is not None]

        # Superseded while waiting for the picture: drop this question.
        if self._current_look != message.job_id:
            return

        if picture:
            label = picture.get("label", "the screen")
            if target and not picture.get("exact", True):
                query = f"(No window matching '{target}' was found; the picture is of the whole screen.) {query}"
            elif label != "the screen":
                query = f"(The picture is of {label}.) {query}"

        await self.queue_frame(
            QuestionFrame(
                query=query,
                image=picture["image"] if picture else None,
                size=tuple(picture["size"]) if picture else None,
                context=[o.for_llm() for o in reversed(recent)],
                windows=[o.for_llm() for o in windows],
            )
        )

        await self._arm_deadline(message.job_id)

    async def _arm_deadline(self, job_id: str):
        await self._disarm_deadline()
        self._deadline = self.create_task(self._expire_look(job_id), name="look-deadline")

    async def _disarm_deadline(self):
        if self._deadline:
            task, self._deadline = self._deadline, None
            await self.cancel_task(task)

    async def _expire_look(self, job_id: str):
        await asyncio.sleep(LOOK_TIMEOUT_SECS)
        self._deadline = None
        if self._current_look != job_id or job_id in self._history_to_look.values():
            return
        logger.warning(f"{self}: look {job_id[:8]} timed out")
        self._current_look = None
        # Stop whatever the model is still doing with it.
        await self.queue_frame(InterruptionFrame())
        await self.send_job_response(
            job_id,
            {"answer": "Sorry, I couldn't get a look at the screen in time."},
            status=JobStatus.ERROR,
            urgent=True,
        )

    async def _fresh_frame(self, target: str = "") -> Optional[dict]:
        """A picture of the target right now, from the screen worker."""
        try:
            async with self.job(
                self._screen_worker,
                params=JobParams(
                    name="frame", payload={"fresh": True, "target": target}, timeout=FRAME_TIMEOUT_SECS
                ),
            ) as t:
                pass
        except JobError as e:
            logger.warning(f"{self}: no picture for this question: {e}")
            return None
        response = t.response or {}
        if not response.get("image"):
            return None
        logger.debug(f"{self}: got frame {response.get('key')} of {response.get('target')}")
        return response

    #
    # Answering
    #

    @tool
    async def start_history_agent(self, params: FunctionCallParams, query: str):
        """Call this function when you don't have enough historical information.

        Args:
            query: The exact question the user is asking.
        """
        logger.debug(f"{self}: asking history: {query}")

        history_id = await self.request_job(
            self._history_worker,
            params=JobParams(name="search", payload={"query": query}),
        )
        if self._current_look:
            self._history_to_look[history_id] = self._current_look

        await params.result_callback(
            "The history search is running. Its answer will be delivered separately. "
            "Do not answer the question yourself."
        )

    async def _on_turn(self, aggregator, message):
        text = (getattr(message, "content", "") or "").strip()
        if not text:
            return

        job_id = self._current_look
        if job_id is None:
            logger.warning(f"{self}: answer with no look job pending: {text}")
            return

        # Text next to a tool call is narration ("let me check the history").
        # Once the question is with the history worker, anything else the
        # model writes is noise: the history worker narrates and answers.
        busy = getattr(aggregator, "has_function_calls_in_progress", False)
        if busy() if callable(busy) else busy:
            await self.send_job_update(job_id, {"say": text}, urgent=True)
            return
        if job_id in self._history_to_look.values():
            logger.debug(f"{self}: dropping text while history searches: {text}")
            return

        self._current_look = None
        await self._disarm_deadline()
        sources = self._look_sources.pop(job_id, [])
        response = {"answer": text}
        if sources:
            response["observation_ids"] = sources
        await self.send_job_response(job_id, response, urgent=True)

    #
    # History results
    #

    async def on_job_update(self, message: BusJobUpdateMessage | BusJobUpdateUrgentMessage):
        await super().on_job_update(message)
        look_id = self._history_to_look.get(message.job_id)
        if look_id:
            await self.send_job_update(look_id, message.update or {}, urgent=True)

    async def on_job_response(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_response(message)
        await self._finish_look_from_history(message)

    async def on_job_error(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_error(message)
        await self._finish_look_from_history(message)

    async def _finish_look_from_history(
        self, message: BusJobResponseMessage | BusJobResponseUrgentMessage
    ):
        look_id = self._history_to_look.pop(message.job_id, None)
        if look_id is None:
            return
        if self._current_look == look_id:
            self._current_look = None
        await self.send_job_response(
            look_id, message.response or {}, status=message.status, urgent=True
        )
