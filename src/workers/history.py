#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
from datetime import date as date_type
from datetime import datetime
from typing import Optional

from loguru import logger
from pipecat.bus.messages import BusJobRequestMessage
from pipecat.frames.frames import LLMMessagesUpdateFrame
from pipecat.pipeline.job_context import JobStatus
from pipecat.pipeline.job_decorator import job
from pipecat.services.llm_service import FunctionCallParams
from pipecat.workers.llm.llm_context_worker import LLMContextWorker
from pipecat.workers.llm.tool_decorator import tool

import models
from store.models import Observation
from store.sqlite_store import SQLiteStore
from workers.names import HISTORY_WORKER

# How long one search may take before the job is failed. Sequential jobs
# queue behind the running one, so this bounds how long a stuck search can
# hold the queue.
SEARCH_TIMEOUT_SECS = 120
# No token for this long from the model ends the request.
STREAM_READ_TIMEOUT_SECS = 60.0

SEARCH_LIMIT = 20
TIMELINE_LIMIT = 100


def system_instruction() -> str:
    now = datetime.now().astimezone()
    return f"""

You are the memory of a screen assistant. You answer questions about what was
on the user's screen in the past, using only the tools below. Right now it is
{now.strftime("%A %Y-%m-%d %H:%M %Z")}.

Be extremely brief. All responses are spoken aloud to the user, so speak to
them directly: "you were in the terminal", never "the user was". Avoid emojis,
bullet points, or anything difficult to vocalize. Answer at a high level, in two or three
sentences at most: name the activities and roughly when, the way a colleague
would sum up a morning, not what was in each window or what each log line
said. The user asks a follow-up when they want more, and only then go into
detail. When the user wants an exact detail, such as a URL, a number, or an
error message, read it out from the verbatim text.

Say times the way a person would: "ten fifteen in the morning", "a quarter
past three in the afternoon", "around five PM"; never seconds, never
24-hour or ISO forms, and no date when it is today.

Every observation is one analyzed screen frame: a time, a description, and the
text that was legible on screen, copied exactly.

Tools:

- search_history(query, since, until): keyword search over descriptions and
  on-screen text, best matches first. Use a few specific words, not a
  sentence. Narrow the time window when the user gives one.

- timeline(since, until): the observations in a time window, oldest first.
  Use it for "what was I doing between nine and ten".

- available_history(date): which hours of a day have observations. Use it
  when you need to know whether there is anything to look at.

Times are ISO 8601 in the user's local time, like 2026-09-01T09:40, and dates
are like 2026-09-01. Start with search_history; fall back to timeline for a
narrow window; say plainly when nothing was found.

"""


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


class HistoryWorker(LLMContextWorker):
    """Answers questions about the past from the store.

    A Pipecat ``LLMContextWorker``: it owns its context and aggregators, and
    the ``@tool`` methods below are its tools, registered from their
    signatures and docstrings. Long-lived. Each ``search`` job resets the
    context to the new question and runs until the model answers without
    calling a tool. Text the model writes alongside a tool call is sent back
    as a progress update, so the requester can narrate "checking this
    morning" while the search runs. The answer carries the ids of the
    observations the model looked at, so an app can show the frames behind
    it.
    """

    def __init__(
        self,
        *,
        store: SQLiteStore,
        max_tokens: int = 16000,
    ):
        self._store = store

        # The job being searched for, how its handler learns the answer, and
        # every observation a tool returned while searching.
        self._job_id: Optional[str] = None
        self._answer: Optional[asyncio.Future[str]] = None
        self._seen_ids: list[int] = []

        # The conversation's model: this worker is its helper. A stream that
        # stalls after its first token once sat for twelve minutes; the
        # client's read timeout turns that into an error the search can report.
        llm = models.make_llm(
            models.current().voice,
            name="HistoryLLMService",
            system_instruction=system_instruction(),
            max_tokens=max_tokens,
            read_timeout_secs=STREAM_READ_TIMEOUT_SECS,
        )
        # Active from the start: activation is what sets the tools.
        super().__init__(HISTORY_WORKER, llm=llm, active=True)

        @self.assistant_aggregator.event_handler("on_assistant_turn_stopped")
        async def _on_turn_stopped(aggregator, message):
            await self._on_turn(aggregator, message)

    @job(name="search", sequential=True)
    async def _search(self, message: BusJobRequestMessage):
        query = str((message.payload or {}).get("query", ""))

        logger.debug(f"{self}: searching history for: {query}")

        self._job_id = message.job_id
        self._answer = asyncio.get_running_loop().create_future()
        self._seen_ids = []

        # A fresh context per search. The system instruction lives in the
        # service settings, so replacing the messages keeps it.
        await self.queue_frame(
            LLMMessagesUpdateFrame(messages=[{"role": "user", "content": query}], run_llm=True)
        )

        try:
            answer = await asyncio.wait_for(self._answer, timeout=SEARCH_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            logger.warning(f"{self}: search timed out after {SEARCH_TIMEOUT_SECS}s")
            await self.send_job_response(
                message.job_id,
                {"answer": "I couldn't finish searching the history."},
                status=JobStatus.ERROR,
                urgent=True,
            )
            return
        finally:
            self._job_id = None
            self._answer = None

        await self.send_job_response(
            message.job_id,
            {"answer": answer, "observation_ids": sorted(set(self._seen_ids))},
            urgent=True,
        )

    async def _on_turn(self, aggregator, message):
        """What the model wrote when its turn ended: narration if it is about
        to call a tool, otherwise the answer."""
        text = (getattr(message, "content", "") or "").strip()
        if not text or self._job_id is None:
            return
        busy = getattr(aggregator, "has_function_calls_in_progress", False)
        if busy() if callable(busy) else busy:
            # Progress, not the answer: the model is about to call a tool.
            await self.send_job_update(self._job_id, {"say": text}, urgent=True)
            return
        if self._answer and not self._answer.done():
            self._answer.set_result(text)

    #
    # Tools
    #

    def _note(self, observations: list[Observation]) -> list[dict]:
        self._seen_ids.extend(o.id for o in observations if o.id is not None)
        return [o.for_llm() for o in observations]

    @tool
    async def search_history(
        self, params: FunctionCallParams, query: str, since: Optional[str] = None, until: Optional[str] = None
    ):
        """Keyword search over what was on screen: descriptions and the exact text that was visible. Best matches first.

        Args:
            query: A few specific keywords, not a full sentence.
            since: ISO 8601 local time, like 2026-09-01T09:40.
            until: ISO 8601 local time, like 2026-09-01T09:40.
        """
        try:
            since_at = _parse_time(since)
            until_at = _parse_time(until)
        except ValueError as e:
            await params.result_callback({"error": f"bad time: {e}"})
            return

        logger.debug(f"{self}: search_history({query!r}, {since_at}, {until_at})")

        found = await self._store.search(query, since=since_at, until=until_at, limit=SEARCH_LIMIT)
        await params.result_callback({"observations": self._note(found)})

    @tool
    async def timeline(self, params: FunctionCallParams, since: str, until: str, limit: Optional[int] = None):
        """The observations in a time window, oldest first.

        Args:
            since: ISO 8601 local time, like 2026-09-01T09:40.
            until: ISO 8601 local time, like 2026-09-01T09:40.
            limit: At most this many, up to 100.
        """
        try:
            since_at = _parse_time(since)
            until_at = _parse_time(until)
        except ValueError as e:
            await params.result_callback({"error": f"bad time: {e}"})
            return
        limit = min(int(limit or TIMELINE_LIMIT), TIMELINE_LIMIT)

        logger.debug(f"{self}: timeline({since_at}, {until_at}, {limit})")

        found = await self._store.timeline(since=since_at, until=until_at, limit=limit)
        await params.result_callback({"observations": self._note(found)})

    @tool
    async def available_history(self, params: FunctionCallParams, date: str):
        """Which hours of a day have observations, with counts.

        Args:
            date: The day, like 2026-09-01.
        """
        try:
            day = date_type.fromisoformat(str(date))
        except ValueError as e:
            await params.result_callback({"error": f"bad date: {e}"})
            return

        logger.debug(f"{self}: available_history({day})")

        coverage = await self._store.coverage(day)
        await params.result_callback(coverage.model_dump())
