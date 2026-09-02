#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Peekaboo on the Mac: the workers against the Mac's own screen, mic, and
speakers. No browser, no server.

    uv run src/app.py

Needs Screen Recording and Microphone granted to the terminal, and the API
keys from ``.env.example`` in a ``.env`` here. The menu bar comes in M4; for
now this is a terminal process, Ctrl-C to quit.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pipecat.workers.runner import WorkerRunner

from macos import permissions
from macos.audio import MacAudioTransport, MacAudioTransportParams
from macos.registry import WindowRegistry
from sources.macos import ScreenCaptureSource
from store.sqlite_store import SQLiteStore
from workers.history import HistoryWorker
from workers.screen import ScreenWorker
from workers.vision import VisionWorker
from workers.voice import VoiceWorker

# Plan §5: the store lives where Mac apps keep their data.
DEFAULT_STORE = Path("~/Library/Application Support/Peekaboo").expanduser()

REQUIRED_KEYS = ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY")


async def main(args) -> int:
    load_dotenv(override=True)

    missing = [k for k in REQUIRED_KEYS if not os.getenv(k)]
    if missing:
        logger.error(f"missing {', '.join(missing)}; copy .env.example to .env and fill it in")
        return 1

    perms = await permissions.request_all()
    if not perms.all_granted:
        return 1

    store = SQLiteStore(root=args.store)
    await store.open()
    await store.prune_images()

    registry = WindowRegistry()
    await registry.start()

    transport = MacAudioTransport(
        MacAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            voice_processing=not args.no_voice_processing,
        )
    )
    source = ScreenCaptureSource(registry=registry)

    # Same workers as bot.py; only the transport and the frame source differ.
    # The screen is read from the OS, so the voice pipeline carries audio only
    # and nothing about the screen crosses a transport.
    runner = WorkerRunner(handle_sigint=True)
    voice = VoiceWorker(transport, screen_from_transport=False, open_links=True, idle_timeout_secs=None)
    screen = ScreenWorker(store=store, source=source)
    vision = VisionWorker(store=store)
    history = HistoryWorker(store=store)
    await runner.add_workers(history, screen, vision, voice)

    @transport.event_handler("on_ready")
    async def on_ready(transport):
        logger.info("audio is up; starting the conversation")
        await voice.start_session("local")

    try:
        await runner.run()
    finally:
        await registry.stop()
        await store.close()
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE, help="where the database and frames live")
    parser.add_argument("--no-voice-processing", action="store_true", help="disable the OS echo canceller")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logger.remove()
    logger.add(sys.stderr, level="TRACE" if args.verbose > 1 else "DEBUG" if args.verbose else "INFO")
    sys.exit(asyncio.run(main(args)))
