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
from pipecat.workers.runner import WorkerRunner
from PyObjCTools import AppHelper

from macos import permissions
from macos.audio import MacAudioTransport, MacAudioTransportParams
from macos.menubar import MenuBar
from macos.registry import WindowRegistry
from sources.macos import ScreenCaptureSource
from store.sqlite_store import SQLiteStore
from workers.history import HistoryWorker
from workers.screen import ScreenWorker
from workers.ui import UIWorker
from workers.vision import VisionWorker
from workers.voice import VoiceWorker

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
        self.runner: Optional[WorkerRunner] = None
        self.ui: Optional[UIWorker] = None

    # --- menu actions, main thread -> asyncio

    def on_pause(self, paused: bool):
        if self.ui:
            asyncio.run_coroutine_threadsafe(self.ui.pause(paused), self.loop)

    def on_unwatch(self, watcher_id: int):
        if self.ui:
            asyncio.run_coroutine_threadsafe(self.ui.unwatch(watcher_id), self.loop)

    def on_quit(self):
        AppKit.NSApp.terminate_(None)

    # --- asyncio side

    async def shutdown(self):
        if self.runner:
            await self.runner.cancel()


    async def run_workers(self) -> int:
        perms = await permissions.request_all()
        if not perms.all_granted:
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
        source = ScreenCaptureSource(registry=registry)

        # Same workers as bot.py plus the ui worker; only the transport and
        # the frame source differ. The screen is read from the OS, so the
        # voice pipeline carries audio only. Signals are AppKit's business on
        # the main thread, so the runner leaves SIGINT alone.
        self.runner = WorkerRunner(handle_sigint=False)
        self.ui = UIWorker(menubar=self.menubar, store=store)
        voice = VoiceWorker(
            transport,
            screen_from_transport=False,
            open_links=True,
            speech="local" if self.args.local_speech else "cloud",
            registry=registry,
            on_state=self.ui.set_voice_state,
            idle_timeout_secs=None,
        )
        screen = ScreenWorker(store=store, source=source)
        vision = VisionWorker(store=store)
        history = HistoryWorker(store=store)
        await self.runner.add_workers(history, screen, vision, voice, self.ui)

        @transport.event_handler("on_ready")
        async def on_ready(transport):
            logger.info("audio is up; starting the conversation")
            await voice.start_session("local")

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

    def applicationShouldTerminate_(self, sender):
        if self.workers is None or self.workers.done():
            return AppKit.NSTerminateNow
        if not self._terminating:
            self._terminating = True
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


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE, help="where the database and frames live")
    parser.add_argument("--no-voice-processing", action="store_true", help="disable the OS echo canceller")
    parser.add_argument(
        "--local-speech",
        action="store_true",
        help="Moonshine and Kokoro on the machine instead of Deepgram and Cartesia",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger.remove()
    logger.add(sys.stderr, level="TRACE" if args.verbose > 1 else "DEBUG" if args.verbose else "INFO")

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

    app = App(args, loop)
    app.menubar = MenuBar(on_pause=app.on_pause, on_quit=app.on_quit, on_unwatch=app.on_unwatch)
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
