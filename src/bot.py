#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pipecat.evals.transport import EvalTransport, EvalTransportParams
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import (
    create_transport,
    get_transport_client_id,
    maybe_capture_participant_screen,
)
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams
from pipecat.workers.runner import WorkerRunner

from sources.transport import TransportScreenSource
from store.sqlite_store import SQLiteStore
from workers.history import HistoryWorker
from workers.screen import ScreenWorker
from workers.vision import VisionWorker
from workers.voice import VoiceWorker

load_dotenv(override=True)


# VAD and turn detection live on the voice worker's user context aggregator,
# so the transports only declare which media they carry. We store functions so
# objects don't get instantiated until the desired transport gets selected.
transport_params = {
    # Headless transport for `pipecat eval` scenarios (see evals/).
    "eval": lambda: EvalTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
    "daily": lambda: DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        video_in_enabled=True,
    ),
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        video_in_enabled=True,
    ),
}


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    store = SQLiteStore(root=Path("db"))
    await store.open()
    await store.prune_images()

    # One runner owns every worker in the session and the bus they talk over.
    # The screen worker watches the screen and keeps the record. The voice
    # worker turns questions into jobs for the vision worker, which asks the
    # screen worker for a picture and the history worker for the past.
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)

    # This entry point runs against a transport (a browser screen share, or
    # the headless eval transport), so the screen comes through it. The Mac
    # app swaps in a source that reads the screen from the OS and a voice
    # worker without the bridge; nothing else changes.
    source = TransportScreenSource()

    voice = VoiceWorker(
        transport,
        screen_from_transport=True,
        # Under the eval transport a meeting link is logged, not opened, and
        # typed turns carry no wake phrase.
        open_links=not isinstance(transport, EvalTransport),
        wake_word=not isinstance(transport, EvalTransport),
        store=store,
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )
    screen = ScreenWorker(store=store, source=source)
    vision = VisionWorker(store=store)
    history = HistoryWorker(store=store)

    await runner.add_workers(history, screen, vision, voice)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected {client}")

        # The client id lets the transport serve screen frame requests, and
        # screen capture has to be on before the first request goes out.
        client_id = get_transport_client_id(transport, client)
        await maybe_capture_participant_screen(transport, client)

        await voice.start_session(client_id)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        # Cancelling the runner takes every worker down together.
        await runner.cancel()

    try:
        await runner.run()
    finally:
        await store.close()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point compatible with Pipecat Cloud."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
