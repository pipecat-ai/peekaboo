#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The window registry: every app and window, kept current by a 1 Hz poll.

ScreenCaptureKit's window list includes windows on other Spaces and off
screen, with titles, and a poll costs 30 to 50 ms. Diffing it by window ID
gives open, close, and retitle events. ``NSWorkspace`` notifications would
add launch and terminate events but need the AppKit run loop, which arrives
with the menu bar in M4.

The pure parts (filtering, diffing, target lookup) take plain dataclasses so
they can be tested without pyobjc.
"""

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Optional

from loguru import logger

POLL_INTERVAL_SECS = 1.0

# Windows smaller than this in points are helpers (cursor, autofill, text
# input popups run 64x64), not content.
MIN_WINDOW_SIZE = 100

# Never captured, never listed. Password managers ship in the denylist.
EXCLUDED_BUNDLES = frozenset(
    {
        "com.1password.1password",
        "com.agilebits.onepassword7",
        "com.agilebits.onepassword-osx",
        "com.bitwarden.desktop",
        "com.lastpass.LastPass",
        "com.dashlane.Dashlane",
        "com.apple.keychainaccess",
        "com.apple.Passwords",
        "com.apple.PasswordsMenuBarExtra",
    }
)


@dataclass(frozen=True)
class Window:
    id: int
    title: str
    app: str
    bundle_id: str
    pid: int
    frame: tuple[float, float, float, float]
    """x, y, width, height in points."""
    on_screen: bool
    layer: int = 0

    @property
    def size(self) -> tuple[float, float]:
        return self.frame[2], self.frame[3]

    def __str__(self):
        x, y, w, h = self.frame
        where = "" if self.on_screen else " offscreen"
        return f"[{self.id}] {self.app}: {self.title!r} {int(w)}x{int(h)}@{int(x)},{int(y)}{where}"


@dataclass(frozen=True)
class App:
    name: str
    bundle_id: str
    pid: int

    def __str__(self):
        return f"{self.name} ({self.bundle_id})"


class EventKind(StrEnum):
    OPENED = "opened"
    CLOSED = "closed"
    RETITLED = "retitled"
    SHOWN = "shown"
    HIDDEN = "hidden"


@dataclass(frozen=True)
class RegistryEvent:
    kind: EventKind
    window: Window
    previous: Optional[Window] = None

    def __str__(self):
        if self.kind == EventKind.RETITLED and self.previous:
            return f"{self.kind} {self.window.app}: {self.previous.title!r} -> {self.window.title!r}"
        return f"{self.kind} {self.window}"


#
# Pure parts
#


def content_windows(
    windows: list[Window],
    *,
    own_pid: int,
    min_size: int = MIN_WINDOW_SIZE,
    excluded: frozenset[str] = EXCLUDED_BUNDLES,
) -> list[Window]:
    """The windows that count: normal layer, big enough, not ours, not denied."""
    return [
        w
        for w in windows
        if w.layer == 0
        and w.pid != own_pid
        and w.bundle_id not in excluded
        and w.frame[2] >= min_size
        and w.frame[3] >= min_size
    ]


def diff(old: dict[int, Window], new: dict[int, Window]) -> list[RegistryEvent]:
    """What changed between two snapshots keyed by window ID."""
    events = []
    for wid, w in new.items():
        prev = old.get(wid)
        if prev is None:
            events.append(RegistryEvent(EventKind.OPENED, w))
        elif prev.title != w.title:
            events.append(RegistryEvent(EventKind.RETITLED, w, prev))
        elif prev.on_screen != w.on_screen:
            events.append(RegistryEvent(EventKind.SHOWN if w.on_screen else EventKind.HIDDEN, w, prev))
    for wid, w in old.items():
        if wid not in new:
            events.append(RegistryEvent(EventKind.CLOSED, w))
    return events


def find_window(windows: list[Window], query: str) -> Optional[Window]:
    """The window best matching ``query`` in its title or app name.

    Exact app-name matches win, then title substrings, then app substrings;
    within a tier the biggest on-screen window wins.
    """
    q = query.strip().lower()
    if not q:
        return None

    def rank(w: Window) -> Optional[tuple]:
        title, app = w.title.lower(), w.app.lower()
        if q == app:
            tier = 0
        elif q in title:
            tier = 1
        elif q in app or q in w.bundle_id.lower():
            tier = 2
        else:
            return None
        return (tier, not w.on_screen, -(w.frame[2] * w.frame[3]))

    ranked = [(r, w) for w in windows if (r := rank(w)) is not None]
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def find_app(apps: list[App], query: str) -> Optional[App]:
    q = query.strip().lower()
    if not q:
        return None
    for app in apps:
        if q == app.name.lower():
            return app
    for app in apps:
        if q in app.name.lower() or q in app.bundle_id.lower():
            return app
    return None


#
# The registry
#


class WindowRegistry:
    """Live view of apps and windows, with events on change.

    Keeps the ``SCWindow`` and ``SCRunningApplication`` objects behind each
    entry so the capture layer can build filters from them.
    """

    def __init__(self, *, interval: float = POLL_INTERVAL_SECS, min_size: int = MIN_WINDOW_SIZE):
        self._interval = interval
        self._min_size = min_size
        self._own_pid = os.getpid()
        self._windows: dict[int, Window] = {}
        self._apps: dict[int, App] = {}
        self._sc_windows: dict[int, object] = {}
        self._sc_apps: dict[int, object] = {}
        self._displays: list = []
        self._listeners: list[Callable[[RegistryEvent], None]] = []
        self._task: Optional[asyncio.Task] = None
        self._refreshed: Optional[asyncio.Event] = None
        self._primed = False

    @property
    def windows(self) -> list[Window]:
        """Front to back, as ScreenCaptureKit lists them."""
        return list(self._windows.values())

    @property
    def apps(self) -> list[App]:
        return sorted(self._apps.values(), key=lambda a: a.name.lower())

    @property
    def displays(self) -> list:
        """``SCDisplay`` objects, main display first."""
        return list(self._displays)

    def sc_window(self, window_id: int):
        return self._sc_windows.get(window_id)

    def sc_app(self, pid: int):
        return self._sc_apps.get(pid)

    def on_event(self, listener: Callable[[RegistryEvent], None]):
        self._listeners.append(listener)

    def find_window(self, query: str) -> Optional[Window]:
        return find_window(self.windows, query)

    def find_app(self, query: str) -> Optional[App]:
        return find_app(self.apps, query)

    def frontmost(self) -> tuple[Optional[App], Optional[Window]]:
        """The active app and its frontmost content window, if any."""
        import AppKit

        running = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if running is None:
            return None, None
        pid = int(running.processIdentifier())
        app = self._apps.get(pid)
        if app is None:
            app = App(
                name=str(running.localizedName() or ""),
                bundle_id=str(running.bundleIdentifier() or ""),
                pid=pid,
            )
        # ScreenCaptureKit lists windows front to back, so the first
        # on-screen one of this app is the one in front.
        window = next((w for w in self._windows.values() if w.pid == pid and w.on_screen), None)
        return app, window

    async def start(self):
        if self._task:
            return
        self._refreshed = asyncio.Event()
        await self.refresh()
        self._task = asyncio.create_task(self._run(), name="window-registry")
        logger.info(f"registry: {len(self._windows)} windows across {len(self._apps)} apps")

    async def stop(self):
        if self._task:
            task, self._task = self._task, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def refresh(self) -> list[RegistryEvent]:
        """Take a snapshot now, fire events for what changed, return them."""
        from macos.capture import shareable_content

        content = await shareable_content()
        self._displays = list(content.displays())

        sc_apps = {}
        apps = {}
        for a in content.applications():
            pid = int(a.processID())
            sc_apps[pid] = a
            apps[pid] = App(name=str(a.applicationName() or ""), bundle_id=str(a.bundleIdentifier() or ""), pid=pid)

        raw: list[Window] = []
        sc_windows = {}
        for w in content.windows():
            app = w.owningApplication()
            frame = w.frame()
            window = Window(
                id=int(w.windowID()),
                title=str(w.title() or ""),
                app=str(app.applicationName() or "") if app else "",
                bundle_id=str(app.bundleIdentifier() or "") if app else "",
                pid=int(app.processID()) if app else 0,
                frame=(frame.origin.x, frame.origin.y, frame.size.width, frame.size.height),
                on_screen=bool(w.isOnScreen()),
                layer=int(w.windowLayer()),
            )
            raw.append(window)
            sc_windows[window.id] = w

        kept = content_windows(raw, own_pid=self._own_pid, min_size=self._min_size)
        new = {w.id: w for w in kept}
        # The first snapshot is the baseline, not forty "opened" events.
        events = diff(self._windows, new) if self._primed else []
        self._primed = True

        self._windows = new
        self._apps = {pid: a for pid, a in apps.items() if a.bundle_id not in EXCLUDED_BUNDLES}
        self._sc_windows = {wid: sc_windows[wid] for wid in new}
        self._sc_apps = sc_apps

        for event in events:
            logger.debug(f"registry: {event}")
            for listener in self._listeners:
                try:
                    listener(event)
                except Exception as e:  # noqa: BLE001 - one listener must not stop the rest
                    logger.warning(f"registry listener failed: {e}")
        if self._refreshed:
            self._refreshed.set()
        return events

    async def _run(self):
        while True:
            t0 = time.monotonic()
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001 - keep polling
                logger.warning(f"registry refresh failed: {e}")
            await asyncio.sleep(max(0.0, self._interval - (time.monotonic() - t0)))


def with_title(window: Window, title: str) -> Window:
    """A copy with another title; handy in tests."""
    return replace(window, title=title)
