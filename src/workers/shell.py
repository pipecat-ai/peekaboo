#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import base64
import inspect
import json
from pathlib import Path
import os
from collections import Counter
import time
import webbrowser
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from loguru import logger
from pipecat.bus.messages import (
    BusJobResponseMessage,
    BusJobResponseUrgentMessage,
    BusJobUpdateMessage,
    BusJobUpdateUrgentMessage,
)
from pipecat.pipeline.job_context import JobError, JobParams, JobStatus
from pipecat.bus.ui.messages import BusUICommandMessage
from pipecat.workers.base_ui_worker import BaseUIWorker

import models
from models import DEFAULT_MODEL_SETTINGS
from macos.memories import MemoriesWindow
from macos.menubar import MenuBar
from store.models import Observation
from store.sqlite_store import SQLiteStore
from workers.names import HISTORY_WORKER, SCREEN_WORKER, SHELL_WORKER, VOICE_WORKER

if TYPE_CHECKING:
    from macos.registry import WindowRegistry

# The store's frames directory is measured for the footer at most this often.
DISK_STATS_SECS = 30.0


# How often the menus are refreshed from the workers and the store.
REFRESH_SECS = 2.0
RECENT_ITEMS = 10


# What a fresh install gets: the system's appearance, and recording from launch.
DEFAULT_SETTINGS = {
    "theme": "system",
    "record_on_launch": True,
    "echo_cancellation": True,
    "input_device": "",
    **DEFAULT_MODEL_SETTINGS,
}


def load_settings(root: Path) -> dict:
    """The saved settings under ``root`` over the defaults."""
    try:
        saved = json.loads((root / "settings.json").read_text())
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULT_SETTINGS, **saved}


class ShellWorker(BaseUIWorker):
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
        registry: Optional["WindowRegistry"] = None,
        screen_worker: str = SCREEN_WORKER,
        history_worker: str = HISTORY_WORKER,
        voice_worker: str = VOICE_WORKER,
        on_listen: Optional[Callable[[bool], Awaitable[None]]] = None,
        on_setting: Optional[Callable[[str, Any], Any]] = None,
        **kwargs,
    ):
        super().__init__(name=SHELL_WORKER, **kwargs)
        # Told of every setting change, for the ones that act on the app
        # (echo cancellation reaches the transport).
        self._on_setting = on_setting
        self._menubar = menubar
        self._store = store
        self._memories = memories
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # The window registry; BaseWorker owns ``_registry`` (the worker registry).
        self._windows = registry
        self._screen_worker = screen_worker
        self._history_worker = history_worker
        self._voice_worker = voice_worker
        self._on_listen = on_listen
        self._paused = False
        self._listening = True
        self._page_ready = False
        self._queued_pushes: list = []
        self._watcher_count = 0
        self._last_watchers: Optional[str] = None
        self._disk: tuple[float, int] = (0.0, 0)
        self._icons: dict[str, str] = {}
        self._refresh: Optional[asyncio.Task] = None
        # Questions the page asked, by history job id.
        self._asks: dict[str, str] = {}

    async def start(self):
        self._loop = asyncio.get_running_loop()
        await super().start()
        self._refresh = asyncio.create_task(self._run_refresh(), name="ui-refresh")
        if self._memories:
            self._memories.set_theme(self._settings().get("theme", "system"))

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
        self._menubar.set_paused(paused)
        self._push("status")

    async def listen(self, on: bool):
        """Mute or unmute the microphone, from the menu or the window."""
        self._listening = on
        if self._on_listen:
            await self._on_listen(on)
        self._menubar.set_listening(on)
        self._push("status")

    async def unwatch(self, watcher_id: int):
        try:
            async with self.job(
                self._screen_worker, params=JobParams(name="unwatch", payload={"id": watcher_id}, timeout=5)
            ):
                pass
        except JobError as e:
            logger.warning(f"{self}: unwatch {watcher_id} failed: {e}")
        await self._refresh_once()

    def _push(self, command: str, payload: Any = None):
        """A command for the page, from any thread: a ``ui-command`` on the
        bus, which the voice worker (owner of the RTVI processor) turns into
        an RTVI envelope for the page's Pipecat client."""
        if self._loop is None:
            return
        message = BusUICommandMessage(
            source=self.name, target=None, command_name=command, payload={} if payload is None else payload
        )
        if not self._page_ready:
            # The window was just opened and its page has not connected yet;
            # a command sent now would be lost. It goes when the page is ready.
            self._queued_pushes.append(message)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._loop.create_task(self.send_bus_message(message))
        else:
            asyncio.run_coroutine_threadsafe(self.send_bus_message(message), self._loop)

    def set_voice_state(self, state: str):
        """From the voice worker's conversation state, any thread."""
        if not self._paused or state != "idle":
            self._menubar.set_state(state)  # type: ignore[arg-type]

    def open_memories(self, ids: Optional[list[int]] = None):
        """Show the memories window, at these observations if given. Any thread."""
        if self._memories:
            self._memories.open()
        if ids:
            self._push("show", {"ids": [int(i) for i in ids]})

    def page_ready(self):
        """The window's page connected: anything pushed meanwhile goes now."""
        self._page_ready = True
        queued, self._queued_pushes = self._queued_pushes, []
        for message in queued:
            self._send_push(message)

    def page_closed(self):
        """The window closed and its page with it."""
        self._page_ready = False

    def _send_push(self, message):
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._loop.create_task(self.send_bus_message(message))
        else:
            asyncio.run_coroutine_threadsafe(self.send_bus_message(message), self._loop)

    def show_ask(self, ask_id: int):
        """Open the window if it is closed and show a past search on the Ask
        screen, as when clicked in Searches. Any thread."""
        self.open_memories()
        self._push("show_ask", {"id": int(ask_id)})

    def show_screen(self, name: str):
        """Open the window if it is closed and switch it to a screen (ask,
        searches, timeline, watchers, settings). Any thread."""
        self.open_memories()
        self._push("navigate", {"view": str(name)})

    def show_asked(self, question: str):
        """A question was asked by voice: the Ask screen shows it and waits.
        Any thread. The window is not opened for it; the answer is there
        when the user looks."""
        self._push("asked", {"question": question})

    async def show_answer(self, question: str, answer: str, ids: list[int]):
        """A spoken answer: kept with the question, and shown with the frames
        behind it on the Ask screen."""
        ask = await self._store.add_ask(question, answer, "voice", ids)
        self._push(
                "ask.answer", await self._answer_payload(answer, ids, ok=True, spoken=True, ask_id=ask.id)
            )

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
        return await self._watchers()

    async def _rpc_watch(self, condition: str, target: str = "", wanted: str = ""):
        """A watcher from the page. The voice worker creates it so its hits
        are spoken, like a watch asked for aloud."""
        try:
            async with self.job(
                self._voice_worker,
                params=JobParams(name="watch", payload={"condition": str(condition), "target": str(target), "wanted": str(wanted or target)}, timeout=10),
            ) as t:
                pass
        except JobError as e:
            raise RuntimeError(f"could not start watching: {e}") from e
        # The voice worker answers once it has asked the screen worker; the
        # watcher itself appears a moment later. Wait for it so the page's
        # next list already has it.
        before = self._watcher_count
        for _ in range(15):
            await asyncio.sleep(0.2)
            watchers = await self._watchers()
            if len(watchers.get("watchers", [])) > before:
                break
        await self._refresh_once()
        return t.response or {}

    async def _rpc_enable_watcher(self, id: int, enabled: bool):
        try:
            async with self.job(
                self._screen_worker,
                params=JobParams(name="enable_watcher", payload={"id": int(id), "enabled": bool(enabled)}, timeout=5),
            ) as t:
                pass
        except JobError as e:
            raise RuntimeError(str(e)) from e
        await self._refresh_once()
        return t.response or {}

    async def _rpc_windows(self):
        """Apps and their titled windows, for the new-watcher picker. Helper
        panels have no title and are left out."""
        from macos.registry import collapse_tabs

        if self._windows is None:
            return []
        by_app: dict[str, dict] = {}
        for w in collapse_tabs(self._windows.windows):
            if not w.title.strip():
                continue
            entry = by_app.setdefault(w.app, {"app": w.app, "bundle_id": w.bundle_id, "windows": []})
            entry["windows"].append({"id": w.id, "title": w.title, "on_screen": w.on_screen, "tabs": len(w.tabs)})
        return sorted(by_app.values(), key=lambda a: a["app"].lower())

    #
    # Settings: a small JSON file in the store root
    #

    def _settings_path(self):
        return self._store.root / "settings.json"

    def _settings(self) -> dict:
        return load_settings(self._store.root)

    def settings(self) -> dict:
        return self._settings()

    def set_recording_state(self, recording: bool):
        """Reflect the state chosen at launch, without sending capture jobs."""
        self._paused = not recording
        self._menubar.set_paused(self._paused)

    async def _rpc_settings(self):
        return self._settings()

    async def _rpc_models(self):
        """The model choices for Settings: providers with their suggested
        models and whether a key is stored, Moonshine's models, Kokoro's
        voices, and what runs right now (a change applies at the next launch)."""
        info = await asyncio.to_thread(models.describe)
        running = models.current()
        info["running"] = {
            "voice_llm": {"provider": running.voice.provider, "model": running.voice.model},
            "vision_llm": {"provider": running.vision.provider, "model": running.vision.model},
            "stt_model": running.stt_model,
            "tts_voice": running.tts_voice,
        }
        return info

    async def _rpc_set_api_key(self, provider: str, key: str):
        """Store a provider's key in the keychain; an empty key removes it."""
        from macos import keychain

        provider = str(provider)
        if provider not in models.PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        key = str(key or "").strip()
        ok = await asyncio.to_thread(keychain.set, provider, key) if key else await asyncio.to_thread(keychain.delete, provider)
        if not ok and key:
            raise RuntimeError("the keychain refused the key")
        logger.info(f"{self}: API key for {provider} {'stored' if key else 'removed'}")
        return {"provider": provider, "has_key": bool(models.api_key(provider))}

    async def _rpc_restart(self):
        """Start the app again so new model choices take effect."""
        logger.info(f"{self}: restarting to apply settings")
        self.create_task(self._restart_soon())
        return {"restarting": True}

    async def _restart_soon(self):
        from macos.permissions import restart

        await asyncio.sleep(0.3)  # the response reaches the page first
        restart()

    async def _rpc_input_devices(self):
        """The microphones present now, for the Settings picker."""
        from macos.audio_devices import input_devices

        return {"devices": [d.describe() for d in await asyncio.to_thread(input_devices)]}

    async def _rpc_set_setting(self, key: str, value):
        settings = self._settings()
        settings[str(key)] = value
        self._settings_path().write_text(json.dumps(settings, indent=2))
        if key == "theme" and self._memories:
            self._memories.set_theme(str(value))
        if self._on_setting:
            result = self._on_setting(str(key), value)
            if inspect.isawaitable(result):
                await result
        return settings

    async def _rpc_stats(self):
        coverage = await self._store.coverage(date.today())
        now = time.monotonic()
        if now - self._disk[0] > DISK_STATS_SECS:
            size = await asyncio.to_thread(_dir_size, self._store.root / "frames")
            self._disk = (now, size)
        return {
            "today": sum(h.count for h in coverage.hours),
            "bytes": self._disk[1],
            "paused": self._paused,
            "listening": self._listening,
            "watching": self._watcher_count,
        }

    async def _rpc_pause(self, paused: bool):
        await self.pause(bool(paused))
        return {"paused": self._paused}

    async def _rpc_listen(self, on: bool):
        await self.listen(bool(on))
        return {"listening": self._listening}

    async def _rpc_month(self, year: int, month: int):
        """Memories per day in a month, for the timeline's calendar."""
        return {"year": int(year), "month": int(month), "days": await self._store.month_counts(int(year), int(month))}

    async def _rpc_day_tracks(self, date: str):
        """The day in one-minute cells. Per hour: the count, and for each
        minute with memories ``{minute, app, apps, ids}``, where ``app`` is the
        one seen most in that minute, ``apps`` the count per app, and ``ids``
        every memory in it. Also the day's apps with counts, for the legend.

        A minute is the unit because a block of the track can then never be
        narrower than a minute whatever the number of apps: the geometry does
        not depend on how often apps alternate."""
        day_start = datetime.combine(_parse_day(date), datetime.min.time())
        rows = await self._store.timeline(since=day_start, until=day_start + timedelta(days=1), limit=5000)
        rows.sort(key=lambda o: (o.timestamp, o.id or 0))
        stills = await self._store.stills(since=day_start, until=day_start + timedelta(days=1))
        t0 = int(day_start.timestamp())
        cells: dict[int, dict] = {}
        totals: Counter = Counter()
        for o in rows:
            minute = (o.timestamp - t0) // 60
            app = o.app or "Screen"
            cell = cells.setdefault(minute, {"minute": minute % 60, "hour": minute // 60, "apps": Counter(), "focus": Counter(), "ids": []})
            cell["apps"][app] += 1
            cell["ids"].append(o.id)
            totals[app] += 1
        # The colour of a minute is the focus: the app in front, from the
        # screen stills. Minutes with content but no still keep the app that
        # changed most, which is what pre-M7 days have.
        for o in stills:
            minute = (o.timestamp - t0) // 60
            if minute in cells and o.app:
                cells[minute]["focus"][o.app] += 1
        hours = []
        for hour in range(24):
            minutes = [
                {
                    "minute": c["minute"],
                    "app": (c["focus"].most_common(1) or c["apps"].most_common(1))[0][0],
                    "apps": dict(c["apps"]),
                    "ids": c["ids"],
                }
                for m, c in sorted(cells.items())
                if c["hour"] == hour
            ]
            hours.append({"hour": hour, "start": t0 + hour * 3600, "count": sum(len(m["ids"]) for m in minutes), "minutes": minutes})
        return {"date": date, "hours": hours, "apps": [{"app": a, "count": n} for a, n in totals.most_common()]}

    async def _rpc_day_frames(self, date: str):
        """Every content memory of the day (window frames), oldest first, for
        the strip and range selection."""
        day_start = datetime.combine(_parse_day(date), datetime.min.time())
        rows = await self._store.timeline(since=day_start, until=day_start + timedelta(days=1), limit=5000)
        rows.sort(key=lambda o: (o.timestamp, o.id or 0))
        return [self._for_page(o) for o in rows]

    async def _rpc_day_stills(self, date: str):
        """The day's screen stills, oldest first: what the timeline scrubs
        through, one per changed tick."""
        day_start = datetime.combine(_parse_day(date), datetime.min.time())
        rows = await self._store.stills(since=day_start, until=day_start + timedelta(days=1))
        return [self._for_page(o) for o in rows]

    async def _rpc_moment(self, moment: int):
        """One recording tick: its screen still and the window frames in it."""
        rows = await self._store.moment(int(moment))
        screen = next((o for o in rows if o.kind == "screen"), None)
        windows = [o for o in rows if o.kind != "screen" and o.target != "screen"]
        return {"moment": int(moment), "screen": self._for_page(screen) if screen else None, "windows": [self._for_page(o) for o in windows]}

    async def _rpc_moments_around(self, moment: int, n: int = 8):
        """The screen stills around a moment, for the viewer's filmstrip."""
        center = datetime.fromtimestamp(int(moment))
        rows = await self._store.stills(since=center - timedelta(hours=1), until=center + timedelta(hours=1))
        index = next((i for i, o in enumerate(rows) if o.moment == int(moment)), None)
        if index is None:
            return []
        return [self._for_page(o) for o in rows[max(0, index - int(n)) : index + int(n) + 1]]

    async def _rpc_around(self, id: int, n: int = 8):
        """The observations around one, for the viewer's filmstrip."""
        found = await self._store.get([int(id)])
        if not found:
            return []
        center = found[0]
        moment = datetime.fromtimestamp(center.timestamp)
        rows = await self._store.timeline(since=moment - timedelta(hours=1), until=moment + timedelta(hours=1), limit=400)
        rows.sort(key=lambda o: (o.timestamp, o.id or 0))
        index = next((i for i, o in enumerate(rows) if o.id == center.id), None)
        if index is None:
            rows, index = [center], 0
        window = rows[max(0, index - int(n)) : index + int(n) + 1]
        return [self._for_page(o) for o in window]

    async def _rpc_open_url(self, url: str):
        url = str(url)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        await asyncio.to_thread(webbrowser.open, url)
        return {"opened": url}

    async def _rpc_app_icons(self, names: list[str]):
        """App icons as data URLs, for running apps the registry knows."""
        if self._windows is None:
            return {}
        out = {}
        for name in names:
            name = str(name)
            if name not in self._icons:
                # Only successes are cached: an app may simply not be in the
                # registry yet (ours, right after its window opened).
                icon = await asyncio.to_thread(self._icon_for, name)
                if icon:
                    self._icons[name] = icon
            if name in self._icons:
                out[name] = self._icons[name]
        return out

    def _icon_for(self, name: str) -> Optional[str]:
        import AppKit

        from macos.registry import OWN_APP_NAME

        if name == OWN_APP_NAME:
            # Ours is set at runtime; NSRunningApplication would show Python's.
            image = AppKit.NSApp.applicationIconImage()
        else:
            app = next((a for a in self._windows.apps if a.name == name), None)
            running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(app.pid) if app else None
            image = running.icon() if running else None
        if image is None:
            return None
        image.setSize_(AppKit.NSMakeSize(64, 64))
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
        return "data:image/png;base64," + base64.b64encode(bytes(png)).decode()

    async def _rpc_unwatch(self, id: int):
        await self.unwatch(int(id))
        return {"ok": True}

    async def _rpc_ask(self, question: str):
        """Hand the question to the history worker. Its narration and answer
        reach the page as events; the call itself returns at once."""
        job_id = await self.request_job(
            self._history_worker, params=JobParams(name="search", payload={"query": str(question)})
        )
        self._asks[job_id] = str(question)
        return {"started": True}

    async def on_job_update(self, message: BusJobUpdateMessage | BusJobUpdateUrgentMessage):
        await super().on_job_update(message)
        if message.job_id in self._asks and self._memories:
            text = (message.update or {}).get("say")
            if text:
                self._push("ask.progress", {"text": text})

    async def on_job_response(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_response(message)
        await self._finish_ask(message)

    async def on_job_error(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_error(message)
        await self._finish_ask(message)

    async def _finish_ask(self, message):
        question = self._asks.pop(message.job_id, None)
        if question is None or not self._memories:
            return
        response = message.response or {}
        ids = [int(i) for i in response.get("observation_ids") or []]
        ok = message.status == JobStatus.COMPLETED
        answer = response.get("answer", "")
        ask_id = None
        if ok and answer:
            ask_id = (await self._store.add_ask(question, answer, "typed", ids)).id
        payload = await self._answer_payload(answer, ids, ok=ok, ask_id=ask_id)
        self._push("ask.answer", payload)

    async def _answer_payload(
        self, answer: str, ids: list[int], *, ok: bool, spoken: bool = False, ask_id: Optional[int] = None
    ) -> dict:
        found = {o.id: o for o in await self._store.get(ids)} if ids else {}
        return {
            "ok": ok,
            "spoken": spoken,
            "ask_id": ask_id,
            "answer": answer,
            "ids": ids,
            "observations": [self._for_page(found[i]) for i in ids if i in found],
        }

    async def _rpc_asks(self, limit: int = 100):
        """Past questions, newest first."""
        return [
            {
                "id": a.id,
                "ts": a.ts,
                "question": a.question,
                "answer": a.answer,
                "source": a.source,
                "count": len(a.observation_ids),
            }
            for a in await self._store.asks(int(limit))
        ]

    async def _rpc_ask_get(self, id: int):
        """One past question with its answer and the frames behind it."""
        ask = await self._store.get_ask(int(id))
        if ask is None:
            return None
        payload = await self._answer_payload(
            ask.answer, ask.observation_ids, ok=True, spoken=ask.source == "voice", ask_id=ask.id
        )
        payload.update({"question": ask.question, "ts": ask.ts, "source": ask.source})
        return payload

    async def _rpc_ask_delete(self, id: int):
        await self._store.delete_ask(int(id))
        return {"deleted": True}

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
            "moment": o.moment,
            "rect": o.rect,
            "target": o.target,
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

    async def _watchers(self) -> dict:
        """``{"watchers": [...], "builtin": {"enabled": bool}}`` from the screen worker."""
        try:
            async with self.job(self._screen_worker, params=JobParams(name="list_watchers", timeout=5)) as t:
                pass
            response = t.response or {}
            return {"watchers": response.get("watchers", []), "builtin": response.get("builtin", {"enabled": True})}
        except JobError:
            return {"watchers": [], "builtin": {"enabled": True}}

    async def _refresh_once(self):
        state = await self._watchers()
        watchers = state["watchers"]
        self._watcher_count = len(watchers)
        self._menubar.set_watchers(watchers)
        # Push to the page only when something changed, so a watcher made by
        # voice shows up there without polling from the page.
        key = json.dumps(state, sort_keys=True)
        if key != self._last_watchers:
            self._last_watchers = key
            self._push("watchers", state)

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


def _dir_size(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
