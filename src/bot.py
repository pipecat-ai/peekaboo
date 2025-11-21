#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from pathlib import Path
from dotenv import load_dotenv
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams

from agent_runner import AgentRunner
from processors.producers import VoiceProducer, VisionProducer
from processors.consumers import VoiceConsumer, VisionConsumer
from vision_agent import VisionAgent
from voice_agent import VoiceAgent
from file_store import PeekabooFileStore

load_dotenv(override=True)


# We store functions so objects (e.g. SileroVADAnalyzer) don't get
# instantiated. The function will be called when the desired transport gets
# selected.
transport_params = {
    "daily": lambda: DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        video_in_enabled=True,
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
        turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams()),
    ),
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        video_in_enabled=True,
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
        turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams()),
    ),
}


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    store = PeekabooFileStore(store_path=Path("db"))

    voice_producer = VoiceProducer()
    vision_producer = VisionProducer()

    voice_consumer = VoiceConsumer(producer=voice_producer)
    vision_consumer = VisionConsumer(producer=vision_producer)

    voice_agent = VoiceAgent(
        transport,
        voice_producer=voice_producer,
        vision_consumer=vision_consumer,
    )

    vision_agent = VisionAgent(
        vision_producer=vision_producer,
        voice_consumer=voice_consumer,
        store=store,
    )

    runner = AgentRunner(handle_sigint=runner_args.handle_sigint)

    await runner.run(voice_agent, vision_agent)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point compatible with Pipecat Cloud."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
