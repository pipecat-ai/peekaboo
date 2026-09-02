#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
from datetime import datetime
from typing import Optional

from loguru import logger
from pipecat.pipeline.job_context import JobError, JobParams
from pipecat.workers.base_worker import BaseWorker

from macos.menubar import MenuBar
from store.sqlite_store import SQLiteStore
from workers.names import SCREEN_WORKER, UI_WORKER

# How often the menus are refreshed from the workers and the store.
REFRESH_SECS = 2.0
RECENT_ITEMS = 10


class UIWorker(BaseWorker):
    """The one crossing between the workers and the menu bar.

    A bus-only worker: no pipeline. It keeps the Watching and Recent menus
    current by asking the screen worker and the store on a cadence, shows the
    voice worker's state, and carries menu actions back as jobs: pause and
    resume stop and start the capture cadence, remove unwatches.

    Polling is enough for a menu; when the menu bar grows a live activity
    view this becomes a bus subscription.
    """

    def __init__(self, *, menubar: MenuBar, store: SQLiteStore, screen_worker: str = SCREEN_WORKER, **kwargs):
        super().__init__(name=UI_WORKER, **kwargs)
        self._menubar = menubar
        self._store = store
        self._screen_worker = screen_worker
        self._paused = False
        self._refresh: Optional[asyncio.Task] = None

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

    async def _refresh_once(self):
        try:
            async with self.job(self._screen_worker, params=JobParams(name="list_watchers", timeout=5)) as t:
                pass
            watchers = (t.response or {}).get("watchers", [])
        except JobError:
            watchers = []
        self._menubar.set_watchers(watchers)

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
