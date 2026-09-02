#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The menu bar: the Pipecat cat as a status item, and the dropdown.

Everything here runs on the main thread. The ``set_*`` methods may be called
from any thread; they hop over with ``AppHelper.callAfter``. Menu actions
call back through plain Python callables on the main thread; the caller
(app.py) hands them to the asyncio loop.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Literal, Optional

import AppKit
import objc
from Foundation import NSObject
from PyObjCTools import AppHelper

State = Literal["idle", "listening", "thinking", "speaking", "paused", "stale"]

ICON = Path(__file__).parent / "assets" / "menubar@2x.png"
ICON_SIZE = (24, 14)

STATE_TOOLTIP = {
    "idle": "Peekaboo",
    "listening": "Peekaboo — listening",
    "thinking": "Peekaboo — thinking",
    "speaking": "Peekaboo — speaking",
    "paused": "Peekaboo — paused",
    "stale": "Peekaboo — can't see a watched window",
}

MAX_RECENT = 10


class _Target(NSObject):
    """Receives menu actions. Selectors only; the logic lives in MenuBar."""

    def initWithMenuBar_(self, menubar):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._menubar = menubar
        return self

    def pause_(self, sender):
        self._menubar._toggle_pause()

    def quit_(self, sender):
        self._menubar._on_quit()

    def unwatch_(self, sender):
        self._menubar._on_unwatch(int(sender.representedObject()))

    def openRecent_(self, sender):
        if self._menubar._on_open_recent:
            self._menubar._on_open_recent(int(sender.representedObject()))

    def search_(self, sender):
        if self._menubar._on_search:
            self._menubar._on_search()


class MenuBar:
    """The status item and its menu. Create on the main thread."""

    def __init__(
        self,
        *,
        on_pause: Callable[[bool], None],
        on_quit: Callable[[], None],
        on_unwatch: Callable[[int], None],
        on_search: Optional[Callable[[], None]] = None,
        on_open_recent: Optional[Callable[[int], None]] = None,
    ):
        self._on_pause = on_pause
        self._on_quit = on_quit
        self._on_unwatch = on_unwatch
        self._on_search = on_search
        self._on_open_recent = on_open_recent
        self._paused = False
        self._state: State = "idle"

        self._target = _Target.alloc().initWithMenuBar_(self)
        self._item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        # A template image: black plus alpha, tinted by the system for light
        # and dark menu bars and for the pressed state.
        icon = AppKit.NSImage.alloc().initWithContentsOfFile_(str(ICON))
        icon.setSize_(AppKit.NSMakeSize(*ICON_SIZE))
        icon.setTemplate_(True)
        self._item.button().setImage_(icon)
        self._item.button().setToolTip_(STATE_TOOLTIP["idle"])

        self._menu = AppKit.NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._add(self._menu, "Open Peekaboo", "search:", key="o").setEnabled_(on_search is not None)
        self._menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._pause_item = self._add(self._menu, "Pause recording", "pause:")
        self._menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._watching_item = self._add(self._menu, "Watching", None)
        self._watching_menu = AppKit.NSMenu.alloc().init()
        self._watching_item.setSubmenu_(self._watching_menu)
        self._recent_item = self._add(self._menu, "Recent", None)
        self._recent_menu = AppKit.NSMenu.alloc().init()
        self._recent_item.setSubmenu_(self._recent_menu)
        self._menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add(self._menu, "Quit Peekaboo", "quit:", key="q")
        self._item.setMenu_(self._menu)

        self.set_watchers([])
        self.set_recent([])

    def _add(self, menu, title: str, action: Optional[str], *, key: str = "", represented=None):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            item.setTarget_(self._target)
        else:
            item.setEnabled_(True)
        if represented is not None:
            item.setRepresentedObject_(represented)
        menu.addItem_(item)
        return item

    #
    # From any thread
    #

    def set_state(self, state: State):
        def go():
            self._state = state
            shown = "paused" if self._paused and state == "idle" else state
            self._item.button().setToolTip_(STATE_TOOLTIP.get(shown, "Peekaboo"))
            self._item.button().setAppearsDisabled_(shown == "paused")

        AppHelper.callAfter(go)

    def set_paused(self, paused: bool):
        """Reflect a pause that came from elsewhere (the memories window)."""

        def go():
            self._paused = paused
            self._pause_item.setTitle_("Start recording" if paused else "Pause recording")
            self.set_state(self._state)

        AppHelper.callAfter(go)

    def set_watchers(self, watchers: list[dict]):
        """``[{"id", "target", "condition"}]`` as the Watching submenu, each
        removable."""

        def go():
            self._watching_menu.removeAllItems()
            if not watchers:
                self._add(self._watching_menu, "Nothing being watched", None).setEnabled_(False)
            for w in watchers:
                self._add(
                    self._watching_menu,
                    f"Stop watching {w.get('target', 'the screen')}: {w.get('condition', '')}",
                    "unwatch:",
                    represented=int(w["id"]),
                )
            self._watching_item.setTitle_(f"Watching ({len(watchers)})" if watchers else "Watching")

        AppHelper.callAfter(go)

    def set_recent(self, items: list[dict]):
        """``[{"id", "time", "app", "content"}]``, newest first."""

        def go():
            self._recent_menu.removeAllItems()
            if not items:
                self._add(self._recent_menu, "Nothing yet", None).setEnabled_(False)
            for o in items[:MAX_RECENT]:
                title = f"{o.get('time', '')}  {o.get('app') or 'Screen'} — {_short(o.get('content', ''), 60)}"
                item = self._add(
                    self._recent_menu, title, "openRecent:" if self._on_open_recent else None, represented=int(o["id"])
                )
                item.setEnabled_(self._on_open_recent is not None)

        AppHelper.callAfter(go)

    #
    # Main thread
    #

    def _toggle_pause(self):
        self._paused = not self._paused
        self._pause_item.setTitle_("Start recording" if self._paused else "Pause recording")
        self.set_state(self._state)
        self._on_pause(self._paused)


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"
