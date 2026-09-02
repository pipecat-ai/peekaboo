#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
from datetime import date, datetime, timedelta
from typing import Any, Optional

from loguru import logger
from pipecat.bus.messages import (
    BusJobResponseMessage,
    BusJobResponseUrgentMessage,
    BusJobUpdateMessage,
    BusJobUpdateUrgentMessage,
)
from pipecat.pipeline.job_context import JobError, JobParams, JobStatus
from pipecat.workers.base_worker import BaseWorker

from macos.memories import MemoriesWindow
from macos.menubar import MenuBar
from store.models import Observation
from store.sqlite_store import SQLiteStore
from workers.names import HISTORY_WORKER, SCREEN_WORKER, UI_WORKER

# How often the menus are refreshed from the workers and the store.
REFRESH_SECS = 2.0
RECENT_ITEMS = 10


class UIWorker(BaseWorker):
    """The one crossing between the workers and the menu bar.

    A bus-only worker: no pipeline. It keeps the Watching and Recent menus
    current by asking the screen worker and the store on a cadence, shows the
    voice worker's state, carries menu actions back as jobs (pause and resume
    stop and start the capture cadence, remove unwatches), and answers the
    memories window's calls: keyword search and browsing straight from the
    store, questions through the history worker with its progress streamed
    to the page.

    Polling is enough for a menu; when the menu bar grows a live activity
    view this becomes a bus subscription.
    """

    def __init__(
        self,
        *,
        menubar: MenuBar,
        store: SQLiteStore,
        memories: Optional[MemoriesWindow] = None,
        screen_worker: str = SCREEN_WORKER,
        history_worker: str = HISTORY_WORKER,
        **kwargs,
    ):
        super().__init__(name=UI_WORKER, **kwargs)
        self._menubar = menubar
        self._store = store
        self._memories = memories
        self._screen_worker = screen_worker
        self._history_worker = history_worker
        self._paused = False
        self._refresh: Optional[asyncio.Task] = None
        # Questions the page asked, by history job id.
        self._asks: set[str] = set()

    async def start(self):
        await super().start()
        self._refresh = asyncio.create_task(self._run_refresh(), name="ui-refresh")

    async def stop(self):
        if self._refresh:
            task, self._refresh = self._refresh, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await super().stop()

    #
    # Menu actions, called on the asyncio loop by app.py
    #

    async def pause(self, paused: bool):
        self._paused = paused
        action = "stop" if paused else "start"
        try:
            async with self.job(
                self._screen_worker, params=JobParams(name="capture", payload={"action": action}, timeout=5)
            ):
                pass
        except JobError as e:
            logger.warning(f"{self}: capture {action} failed: {e}")
        logger.info(f"{self}: {'paused' if paused else 'resumed'}")
        self._menubar.set_state("paused" if paused else "idle")

    async def unwatch(self, watcher_id: int):
        try:
            async with self.job(
                self._screen_worker, params=JobParams(name="unwatch", payload={"id": watcher_id}, timeout=5)
            ):
                pass
        except JobError as e:
            logger.warning(f"{self}: unwatch {watcher_id} failed: {e}")
        await self._refresh_once()

    def set_voice_state(self, state: str):
        """From the voice worker's conversation state, any thread."""
        if not self._paused or state != "idle":
            self._menubar.set_state(state)  # type: ignore[arg-type]

    def open_memories(self, ids: Optional[list[int]] = None):
        """Show the memories window, at these observations if given. Any thread."""
        if self._memories:
            self._memories.open(ids)

    #
    # The memories window's calls
    #

    async def call(self, method: str, params: dict) -> Any:
        """Dispatch a page call. Runs on the loop; results are JSON."""
        handler = getattr(self, f"_rpc_{method}", None)
        if handler is None:
            raise ValueError(f"unknown method {method!r}")
        return await handler(**params)

    async def _rpc_recent(self, limit: int = 30):
        return [self._for_page(o) for o in await self._store.recent(limit=int(limit))]

    async def _rpc_get(self, ids: list[int]):
        found = {o.id: o for o in await self._store.get([int(i) for i in ids])}
        return [self._for_page(found[i]) for i in ids if i in found]

    async def _rpc_search(self, query: str, limit: int = 30):
        return [self._for_page(o) for o in await self._store.search(str(query), limit=int(limit))]

    async def _rpc_day(self, date: str):
        coverage = await self._store.coverage(_parse_day(date))
        return {"date": coverage.date, "hours": [{"hour": h.hour, "count": h.count} for h in coverage.hours]}

    async def _rpc_hour(self, date: str, hour: int):
        start = datetime.combine(_parse_day(date), datetime.min.time()) + timedelta(hours=int(hour))
        rows = await self._store.timeline(since=start, until=start + timedelta(hours=1), limit=200)
        rows.sort(key=lambda o: (o.timestamp, o.id or 0), reverse=True)
        return [self._for_page(o) for o in rows]

    async def _rpc_watchers(self):
        return {"watchers": await self._watchers()}

    async def _rpc_unwatch(self, id: int):
        await self.unwatch(int(id))
        return {"ok": True}

    async def _rpc_ask(self, question: str):
        """Hand the question to the history worker. Its narration and answer
        reach the page as events; the call itself returns at once."""
        job_id = await self.request_job(
            self._history_worker, params=JobParams(name="search", payload={"query": str(question)})
        )
        self._asks.add(job_id)
        return {"started": True}

    async def on_job_update(self, message: BusJobUpdateMessage | BusJobUpdateUrgentMessage):
        await super().on_job_update(message)
        if message.job_id in self._asks and self._memories:
            text = (message.update or {}).get("say")
            if text:
                self._memories.send("ask.progress", {"text": text})

    async def on_job_response(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_response(message)
        await self._finish_ask(message)

    async def on_job_error(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_error(message)
        await self._finish_ask(message)

    async def _finish_ask(self, message):
        if message.job_id not in self._asks:
            return
        self._asks.discard(message.job_id)
        if not self._memories:
            return
        response = message.response or {}
        ids = [int(i) for i in response.get("observation_ids") or []]
        found = {o.id: o for o in await self._store.get(ids)} if ids else {}
        self._memories.send(
            "ask.answer",
            {
                "ok": message.status == JobStatus.COMPLETED,
                "answer": response.get("answer", ""),
                "ids": ids,
                "observations": [self._for_page(found[i]) for i in ids if i in found],
            },
        )

    def _for_page(self, o: Observation) -> dict:
        def url(path: Optional[str]) -> Optional[str]:
            return self._store.resolve(path).as_uri() if path else None

        return {
            "id": o.id,
            "ts": o.timestamp,
            "app": o.app,
            "title": o.title,
            "kind": o.kind,
            "content": o.content,
            "verbatim_text": o.verbatim_text,
            "screenshot_url": url(o.screenshot_path),
            "thumbnail_url": url(o.thumbnail_path),
        }

    #
    # Refresh
    #

    async def _run_refresh(self):
        while True:
            try:
                await self._refresh_once()
            except Exception as e:  # noqa: BLE001 - a failed refresh is not fatal
                logger.debug(f"{self}: refresh failed: {e}")
            await asyncio.sleep(REFRESH_SECS)

    async def _watchers(self) -> list[dict]:
        try:
            async with self.job(self._screen_worker, params=JobParams(name="list_watchers", timeout=5)) as t:
                pass
            return (t.response or {}).get("watchers", [])
        except JobError:
            return []

    async def _refresh_once(self):
        self._menubar.set_watchers(await self._watchers())

        recent = await self._store.recent(limit=RECENT_ITEMS)
        self._menubar.set_recent(
            [
                {
                    "id": o.id,
                    "time": datetime.fromtimestamp(o.timestamp).strftime("%H:%M"),
                    "app": o.app,
                    "content": o.content,
                }
                for o in recent
            ]
        )


def _parse_day(text: str) -> date:
    return date.fromisoformat(str(text)[:10])
