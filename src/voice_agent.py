#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.runner.utils import (
    get_transport_client_id,
    maybe_capture_participant_screen,
)
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport

from base_agent import BaseAgent
from processors.consumers import VisionConsumer
from processors.frames import VisionQueryFrame, VoiceAgentStartedFrame, VoiceAgentStoppedFrame
from processors.producers import VoiceProducer

SYSTEM_INSTRUCTION = """

You are a voice assistant. Using a tool call, you have access to the user screen
and also to historical data about the screen.

Tool-use rules:

- If the user asks a general knwoledge question, DO NOT call any tool.

- If the user asks about something that could be on the screen (now or in the
  past), call [get_vision_help]. NEVER provide an answer at this point.

- If unsure, ALWAYS double-check with the user before calling any tool.

Be extremely brief. All responses are spoken aloud. Avoid emojis, bullet points,
or anything difficult to vocalize.

"""


class VoiceAgent(BaseAgent):
    def __init__(
        self,
        transport: BaseTransport,
        voice_producer: VoiceProducer,
        vision_consumer: VisionConsumer,
    ):
        self._transport = transport
        self._voice_producer = voice_producer
        self._vision_consumer = vision_consumer

    async def create_task(self) -> PipelineTask:
        stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))

        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",  # British Reading Lady
        )

        llm = AnthropicLLMService(
            name="VoiceAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
        )
        llm.register_function("get_vision_help", self._get_vision_help)

        vision_function = FunctionSchema(
            name="get_vision_help",
            description=(
                "Call this function whenever the user asks about something on their screen, "
                "either something currently visible or something they expect to appear later. "
                "This includes questions about UI elements, text, images, buttons, errors."
            ),
            properties={
                "query": {
                    "type": "string",
                    "description": "The exact question the user is asking.",
                },
                "watchlist": {
                    "type": "boolean",
                    "description": (
                        "Set to true if the user wants to be notified repeatedly whenever "
                        "a relevant visual event occurs (e.g., when a window appears, "
                        "a button becomes enabled, a value changes, etc.)."
                    ),
                },
            },
            required=["query", "watchlist"],
        )

        tools = ToolsSchema(standard_tools=[vision_function])

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            },
        ]

        context = LLMContext(messages, tools)
        context_aggregator = LLMContextAggregatorPair(context)

        pipeline = Pipeline(
            [
                self._transport.input(),  # Transport user input
                self._voice_producer,  # Send frames to the vision agent
                self._vision_consumer,  # Receives frames from the vision agent
                stt,
                context_aggregator.user(),  # User spoken responses
                llm,  # LLM
                tts,  # TTS
                self._transport.output(),  # Transport bot output
                context_aggregator.assistant(),  # Assistant spoken responses and tool context
            ]
        )

        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            idle_timeout_secs=None,
        )

        @self._transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Client connected {client}")

            # Get the participant ID so we can request images when the vision
            # agent requests them.
            client_id = get_transport_client_id(transport, client)
            self._vision_consumer.set_user_id(client_id)

            # Enable screen capture. We will request frames periodically in our
            # producer processor.
            await maybe_capture_participant_screen(transport, client)

            # Kick off the conversation.
            messages.append({"role": "system", "content": "Ask the user how can you help."})
            await task.queue_frames([VoiceAgentStartedFrame(), LLMRunFrame()])

        @self._transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info(f"Client disconnected")
            await task.queue_frame(VoiceAgentStoppedFrame())
            await task.cancel()

        return task

    async def _get_vision_help(self, params: FunctionCallParams):
        query = params.arguments["query"]
        watchlist = params.arguments["watchlist"]

        await self._voice_producer.queue_frame(VisionQueryFrame(query=query, watchlist=watchlist))

        result = (
            "Just tell the user you will let them know. DO NOT provide an answer."
            if watchlist
            else "Just tell the user to wait for a second. DO NOT provide an answer."
        )
        await params.result_callback(result)
