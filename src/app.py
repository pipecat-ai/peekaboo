#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Peekaboo on the Mac: a menu bar app over the workers, against the Mac's own
screen, mic, and speakers. No browser, no server.

    uv run src/app.py

AppKit owns the main thread; the workers run on an asyncio loop on a
background thread. The two meet in the ``ui`` worker and the menu bar. Quit
from the menu, Ctrl-C in the terminal, or log out: every exit goes through
``NSApp.terminate``, and the app delegate holds termination until the workers
are down.

Needs Screen Recording and Microphone granted to the terminal, and the keys
from ``.env.example`` in a ``.env`` here. With ``--local-speech`` recognition
and synthesis run on the machine (Moonshine, Kokoro; models download on first
use) and only the Anthropic key is needed.
"""

import argparse
import asyncio
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

import AppKit
import objc
from dotenv import load_dotenv
from Foundation import NSObject, NSTimer
from loguru import logger
from pipecat.pipeline.job_context import JobParams
from pipecat.workers.runner import WorkerRunner
from PyObjCTools import AppHelper

from macos import permissions
from macos.audio import MacAudioTransport, MacAudioTransportParams
from macos.memories import MemoriesWindow
from macos.menubar import MenuBar
from macos.registry import WindowRegistry
from sources.macos import ScreenCaptureSource
from store.sqlite_store import SQLiteStore
from workers.history import HistoryWorker
from workers.screen import ScreenWorker
from workers.names import UI_WORKER
from workers.shell import ShellWorker
from workers.ui import PeekabooUIWorker
from workers.vision import VisionWorker
from workers.voice import VoiceWorker

APP_ICON = Path(__file__).parent / "macos" / "assets" / "appicon.png"

# Plan §5: the store lives where Mac apps keep their data.
DEFAULT_STORE = Path("~/Library/Application Support/Peekaboo").expanduser()

# With --local-speech only the LLM needs a key.
CLOUD_SPEECH_KEYS = ("DEEPGRAM_API_KEY", "CARTESIA_API_KEY")


class App:
    """Holds what the menu bar needs to reach on the asyncio side."""

    def __init__(self, args, loop: asyncio.AbstractEventLoop):
        self.args = args
        self.loop = loop
        self.menubar: Optional[MenuBar] = None
        self.memories: Optional[MemoriesWindow] = None
        self.runner: Optional[WorkerRunner] = None
        self.shell: Optional[ShellWorker] = None

    # --- menu actions, main thread -> asyncio

    def on_pause(self, paused: bool):
        if self.shell:
            asyncio.run_coroutine_threadsafe(self.shell.pause(paused), self.loop)

    def on_listen(self, on: bool):
        if self.shell:
            asyncio.run_coroutine_threadsafe(self.shell.listen(on), self.loop)

    def on_unwatch(self, watcher_id: int):
        if self.shell:
            asyncio.run_coroutine_threadsafe(self.shell.unwatch(watcher_id), self.loop)

    def on_quit(self):
        AppKit.NSApp.terminate_(None)

    def on_search(self):
        if self.memories:
            self.memories.open()

    def on_open_recent(self, observation_id: int):
        if self.memories:
            self.memories.open([observation_id])

    # --- asyncio side

    async def shutdown(self):
        if self.runner:
            await self.runner.cancel()


    async def run_workers(self) -> int:
        perms = await permissions.request_all()
        if not perms.all_granted:
            if permissions.bundled() and perms.microphone and not perms.screen_recording and not permissions.is_relaunch():
                # The dialog is up; it can only open System Settings, where the
                # user turns Peekaboo on. The grant applies to a fresh process,
                # so wait for the switch and relaunch once.
                logger.info("waiting for the Screen Recording switch to be turned on for Peekaboo")
                if await permissions.wait_for_screen_recording():
                    permissions.relaunch()
            return 1

        store = SQLiteStore(root=self.args.store)
        await store.open()
        await store.prune_images()

        registry = WindowRegistry()
        await registry.start()

        transport = MacAudioTransport(
            MacAudioTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                voice_processing=not self.args.no_voice_processing,
            )
        )
        if self.memories:
            # The window's page is the pipeline's RTVI client: envelopes out
            # through the web view, the page's messages in to the transport.
            transport.set_client(self.memories.send_rtvi)
            self.memories.on_rtvi = transport.receive_message
        source = ScreenCaptureSource(registry=registry)

        # Same workers as bot.py plus the ui worker; only the transport and
        # the frame source differ. The screen is read from the OS, so the
        # voice pipeline carries audio only. Signals are AppKit's business on
        # the main thread, so the runner leaves SIGINT alone.
        self.runner = WorkerRunner(handle_sigint=False)
        self.shell = ShellWorker(
            menubar=self.menubar, store=store, memories=self.memories, registry=registry, on_listen=lambda on: voice.set_listening(on)
        )
        voice = VoiceWorker(
            transport,
            screen_from_transport=False,
            open_links=True,
            speech="local" if self.args.local_speech else "cloud",
            registry=registry,
            on_state=self.shell.set_voice_state,
            on_show=self.shell.open_memories,
            on_asked=self.shell.show_asked,
            on_show_ask=self.shell.show_ask,
            on_show_screen=self.shell.show_screen,
            on_open_window=self.shell.open_memories,
            store=store,
            on_answer=self.shell.show_answer,
            on_recording=lambda on: self.shell.pause(not on),
            greeting_cache=self.args.store / "greetings",
            idle_timeout_secs=None,
        )
        if voice.rtvi:
            # The page's data requests (client-message) are answered by the
            # shell worker; the response goes back as a server-response.
            @voice.rtvi.event_handler("on_client_message")
            async def on_client_message(rtvi, message):
                try:
                    result = await self.shell.call(message.type, dict(message.data or {}))
                    await rtvi.send_server_response(message, result)
                except Exception as e:  # noqa: BLE001 - reported to the page
                    logger.warning(f"page request {message.type} failed: {e}")
                    await rtvi.send_error_response(message, str(e))

        screen = ScreenWorker(store=store, source=source)
        vision = VisionWorker(store=store)
        history = HistoryWorker(store=store)
        ui = PeekabooUIWorker()
        await self.runner.add_workers(history, screen, vision, voice, self.shell, ui)
        # Development hooks: open the page, poke it, picture it.
        dev = self.args.open_memories or self.args.snapshot_memories or self.args.memories_eval or self.args.window_request
        if dev and self.memories:
            self.memories.open()

            async def dev_later():
                await asyncio.sleep(4)
                if self.args.memories_eval:
                    self.memories.evaluate(self.args.memories_eval)
                if self.args.window_request:
                    await asyncio.sleep(3)  # the page's snapshot stream is up by then
                    await voice.request_job(
                        UI_WORKER, params=JobParams(name="respond", payload={"query": self.args.window_request})
                    )
                    await asyncio.sleep(20)
                if self.args.snapshot_memories:
                    self.memories.snapshot(self.args.snapshot_memories)

            asyncio.get_running_loop().create_task(dev_later())

        @transport.event_handler("on_ready")
        async def on_ready(transport):
            recording = self.shell.settings().get("record_on_launch", True)
            logger.info(f"audio is up; starting the conversation ({'recording' if recording else 'not recording'})")
            self.shell.set_recording_state(recording)
            await voice.start_session("local", recording=recording)

        try:
            await self.runner.run()
        finally:
            await registry.stop()
            await store.close()
        return 0


class _Delegate(NSObject):
    """Holds termination until the workers have been cancelled."""

    def initWithApp_(self, app):
        self = objc.super(_Delegate, self).init()
        if self is None:
            return None
        self._app = app
        self.workers = None
        self._terminating = False
        return self

    def applicationDidFinishLaunching_(self, notification):
        # Set again once launched: an icon set before launch can be replaced
        # by the default when the app finishes starting up.
        icon = AppKit.NSImage.alloc().initWithContentsOfFile_(str(APP_ICON))
        if icon is not None:
            AppKit.NSApp.setApplicationIconImage_(icon)
        # The status item goes in now: created earlier it can land off
        # screen when LaunchServices starts the app.
        if self._app.menubar is not None:
            self._app.menubar.install()

    def applicationShouldTerminate_(self, sender):
        if self.workers is None or self.workers.done():
            return AppKit.NSTerminateNow
        if not self._terminating:
            self._terminating = True
            if self._app.runner is None:
                # Still before the workers exist: waiting for a permission.
                # Cancel that wait; the future's callback finishes the quit.
                logger.info("quitting before the workers started")
                self.workers.cancel()
            else:
                logger.info("quitting; taking the workers down")
                asyncio.run_coroutine_threadsafe(self._app.shutdown(), self._app.loop)
        return AppKit.NSTerminateLater

    @objc.python_method
    def workers_finished(self, future):
        # On the main thread. Either the app is quitting and waited for this,
        # or the workers stopped on their own and the app follows.
        if self._terminating:
            AppKit.NSApp.replyToApplicationShouldTerminate_(True)
        else:
            AppKit.NSApp.terminate_(None)

    def tick_(self, timer):
        # Nothing to do: the timer exists so Python signal handlers get to run
        # while AppKit owns the main thread.
        pass


def build_main_menu():
    """The menu bar a regular app shows next to the Apple menu while it has a
    window: the app menu, Edit (so the page's text fields get cut, copy, and
    paste), and Window."""

    def item(title, action, key, modifiers=None):
        it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if modifiers is not None:
            it.setKeyEquivalentModifierMask_(modifiers)
        return it

    main = AppKit.NSMenu.alloc().init()

    app_menu = AppKit.NSMenu.alloc().initWithTitle_("Peekaboo")
    app_menu.addItem_(item("Hide Peekaboo", "hide:", "h"))
    app_menu.addItem_(item("Hide Others", "hideOtherApplications:", "h", AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagOption))
    app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    app_menu.addItem_(item("Quit Peekaboo", "terminate:", "q"))
    holder = AppKit.NSMenuItem.alloc().init()
    holder.setSubmenu_(app_menu)
    main.addItem_(holder)

    edit = AppKit.NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in [
        ("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), (None, None, None),
        ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a"),
    ]:
        edit.addItem_(AppKit.NSMenuItem.separatorItem() if title is None else item(title, action, key))
    holder = AppKit.NSMenuItem.alloc().init()
    holder.setSubmenu_(edit)
    main.addItem_(holder)

    window = AppKit.NSMenu.alloc().initWithTitle_("Window")
    window.addItem_(item("Close", "performClose:", "w"))
    window.addItem_(item("Minimize", "performMiniaturize:", "m"))
    holder = AppKit.NSMenuItem.alloc().init()
    holder.setSubmenu_(window)
    main.addItem_(holder)
    return main


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE, help="where the database and frames live")
    parser.add_argument("--no-voice-processing", action="store_true", help="disable the OS echo canceller")
    parser.add_argument(
        "--local-speech",
        action="store_true",
        help="Moonshine and Kokoro on the machine instead of Deepgram and Cartesia",
    )
    parser.add_argument("--open-memories", action="store_true", help="open the memories window on launch")
    parser.add_argument("--snapshot-memories", type=Path, help="write a PNG of the memories page after launch")
    parser.add_argument("--memories-eval", metavar="JS", help="run JavaScript in the memories page after launch")
    parser.add_argument("--window-request", metavar="TEXT", help="hand TEXT to the window agent after launch, as if said by voice")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger.remove()
    # -v/-vv from the command line; PEEKABOO_LOG from the bundle's environment.
    level = os.environ.get("PEEKABOO_LOG") or ("TRACE" if args.verbose > 1 else "DEBUG" if args.verbose else "INFO")
    logger.add(sys.stderr, level=level)

    load_dotenv(override=True)
    required = ("ANTHROPIC_API_KEY",) + (() if args.local_speech else CLOUD_SPEECH_KEYS)
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logger.error(f"missing {', '.join(missing)}; copy .env.example to .env and fill it in")
        return 1

    # asyncio on its own thread, up before AppKit takes the main thread.
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="asyncio", daemon=True)
    thread.start()

    ns_app = AppKit.NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # menu bar only
    # Until there is a bundle: the cat as the icon in the Dock and Cmd-Tab
    # (shown while a window is open), and our name where AppKit reads it.
    icon = AppKit.NSImage.alloc().initWithContentsOfFile_(str(APP_ICON))
    if icon is not None:
        ns_app.setApplicationIconImage_(icon)
    info = AppKit.NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info["CFBundleName"] = "Peekaboo"

    ns_app.setMainMenu_(build_main_menu())

    app = App(args, loop)
    app.memories = MemoriesWindow(store_root=args.store, loop=loop)
    app.menubar = MenuBar(
        on_pause=app.on_pause,
        on_listen=app.on_listen,
        on_quit=app.on_quit,
        on_unwatch=app.on_unwatch,
        on_search=app.on_search,
        on_open_recent=app.on_open_recent,
    )
    delegate = _Delegate.alloc().initWithApp_(app)
    ns_app.setDelegate_(delegate)

    # The workers run for the life of the app; when they stop, so does it.
    workers = asyncio.run_coroutine_threadsafe(app.run_workers(), loop)
    delegate.workers = workers
    workers.add_done_callback(lambda f: AppHelper.callAfter(delegate.workers_finished, f))

    # Ctrl-C quits like the menu does. The handler is Python, so it only runs
    # when the interpreter gets control: the timer makes sure it does.
    signal.signal(signal.SIGINT, lambda *_: AppHelper.callAfter(AppKit.NSApp.terminate_, None))
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, delegate, "tick:", None, True)

    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
