#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os

from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.services.anthropic.llm import AnthropicLLMService

from base_agent import BaseAgent
from processors.consumers import VoiceConsumer
from processors.producers import VisionProducer
from processors.vision import (
    VisionImageContextProcessor,
    VisionImageProcessor,
    VisionQueryContextProcessor,
    VisionQueryProcessor,
)

QUERY_SYSTEM_INSTRUCTION = """

You are a vision agent helper. The user context mostly contains JSON objects like
the following:

  {"type": "description", "content": "Image description.", "timestamp": 1763680389 }

ALWAYS use the "content" field to answer the user’s question. Do not rely on outside
knowledge, just look at the context. Accuracy is critical.

All responses must be very brief and easy to speak aloud. Do not use emojis,
bullet points, or symbols that are difficult to vocalize.

"""

IMAGE_SYSTEM_INSTRUCTION = """

You are a vision agent. You will be given images and must analyze them.

Your output must be a JSON object with the following fields:

FIELDS

1. "type"
   Specifies the kind of output. Must be one of:
     - "description": The image does not contain anything relevant to the watchlist.
     - "watchlist": The image contains something relevant to the watchlist.

2. "content"
   A concise description of the image or a direct response to the user’s query.
   Your responses are spoken aloud. Avoid emojis, bullet points, or any symbols
   that are difficult to vocalize. Speak naturally, clearly, and in full sentences
   that are easy to understand.

3. "timestamp"
   The UTC Unix timestamp (in seconds) when the image was received.

4. "watchlist" (optional)
   Include this field only when "type" is "watchlist".
   Its value must be a list of watchlist item numbers detected in the image.

WATCHLIST RULES

- When "type" is "watchlist", the "content" must be extremely brief.

WATCHLIST ITEMS:
"""

IMAGE_OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "content": {"type": "string"},
            "watchlist": {"type": "array"},
            "timestamp": {"type": "integer"},
        },
        "required": ["type", "content", "timestamp"],
        "additionalProperties": False,
    },
}


class VisionAgent(BaseAgent):
    def __init__(self, *, vision_producer: VisionProducer, voice_consumer: VoiceConsumer):
        self._vision_producer = vision_producer
        self._voice_consumer = voice_consumer

    async def create_task(self) -> PipelineTask:
        # Query branch
        query_llm = AnthropicLLMService(
            name="VisionQueryAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
        )

        query_context = LLMContext()
        query_context_aggregator = LLMContextAggregatorPair(query_context)
        query_processor = VisionQueryProcessor(system_instruction=QUERY_SYSTEM_INSTRUCTION)
        query_context_processor = VisionQueryContextProcessor()

        # Vision branch
        image_llm = AnthropicLLMService(
            name="VisionImageAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            params=AnthropicLLMService.InputParams(
                extra={
                    "extra_headers": {
                        "anthropic-beta": "structured-outputs-2025-11-13",
                    },
                    "extra_body": {
                        "output_format": IMAGE_OUTPUT_FORMAT,
                    },
                }
            ),
        )

        image_context = LLMContext()
        image_context_aggregator = LLMContextAggregatorPair(image_context)
        image_processor = VisionImageProcessor(system_instruction=IMAGE_SYSTEM_INSTRUCTION)
        image_context_processor = VisionImageContextProcessor(query_processor=query_processor)

        pipeline = Pipeline(
            [
                self._voice_consumer,  # Receives frames the voice agent
                ParallelPipeline(
                    [
                        query_processor,
                        query_context_aggregator.user(),
                        query_llm,
                        query_context_aggregator.assistant(),
                        query_context_processor,
                    ],
                    [
                        image_processor,
                        image_context_aggregator.user(),
                        image_llm,
                        image_context_aggregator.assistant(),
                        image_context_processor,
                    ],
                ),
                self._vision_producer,  # Sends frames the voice agent
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

        return task
