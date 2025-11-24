#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Dict

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.llm_service import FunctionCallParams

from agent_runner import AgentRunner
from base_agent import BaseAgent
from base_store import BaseStore, ImageRecord
from history_agent import HistoryAgent
from processors.consumers import VoiceConsumer
from processors.producers import VisionProducer
from processors.vision import (
    VisionImageContextProcessor,
    VisionImageProcessor,
    VisionQueryContextProcessor,
    VisionQueryProcessor,
)

today = datetime.now().astimezone().strftime("%B %d, %Y %Z")

QUERY_SYSTEM_INSTRUCTION = f"""

You are a vision agent helper. Today is {today}. Your context contains
historical screen information, but it might not be complete. ALWAYS use the
[start_history_agent] tool if you do NOT have enough historical data.

The user context contains JSON objects like the following:

  {{"type": "description", "content": "Image description.", "timestamp": 1763680389 }}

ALWAYS use the "content" field to answer the user’s question. Do not rely on
outside knowledge, just look at the context. Accuracy is critical.

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
   The Unix timestamp (in seconds) when the image was received.

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
    def __init__(
        self,
        *,
        vision_producer: VisionProducer,
        voice_consumer: VoiceConsumer,
        store: BaseStore,
    ):
        self._vision_producer = vision_producer
        self._voice_consumer = voice_consumer
        self._store = store
        self._history_agent_runners: Dict[str, AgentRunner] = {}
        self._history_agent_tasks: Dict[str, asyncio.Task] = {}

    async def create_task(self) -> PipelineTask:
        # Query branch
        query_llm = AnthropicLLMService(
            name="VisionQueryAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
        )
        query_llm.register_function("start_history_agent", self._start_history_agent)

        history_function = FunctionSchema(
            name="start_history_agent",
            description="Call this function when you don't have enough historical information.",
            properties={
                "query": {
                    "type": "string",
                    "description": "The exact question the user is asking.",
                }
            },
            required=["query"],
        )

        query_tools = ToolsSchema(standard_tools=[history_function])

        query_context = LLMContext(tools=query_tools)
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

        @image_context_processor.event_handler("on_image_analysis")
        async def on_image_analysis(processor, data: dict):
            record = ImageRecord.model_validate(data)
            await self._store.append(record)

        @task.event_handler("on_pipeline_finished")
        async def on_pipeline_finished(task, frame):
            for id, r in self._history_agent_runners.items():
                await r.cancel()

            # Wait for all tasks/runners to finish.
            tasks = self._history_agent_tasks.values()
            await asyncio.gather(*tasks)

        return task

    async def _start_history_agent(self, params: FunctionCallParams):
        query = params.arguments["query"]

        agent_id = str(uuid.uuid4())

        logger.debug(f"Starting history agent {agent_id} with query: {query}")

        runner = AgentRunner(handle_sigint=False)

        agent = HistoryAgent(
            id=agent_id,
            query=query,
            response_processor=self._vision_producer,
            store=self._store,
        )

        task = asyncio.create_task(self._history_agent_task_handler(runner, agent))
        task.set_name(agent_id)
        task.add_done_callback(self._history_agent_task_done)

        self._history_agent_runners[agent_id] = runner
        self._history_agent_tasks[agent_id] = task

        await params.result_callback("History agent started.")

    async def _history_agent_task_handler(self, runner: AgentRunner, agent: BaseAgent):
        await runner.run(agent)

    def _history_agent_task_done(self, task: asyncio.Task):
        if task.get_name() in self._history_agent_tasks:
            del self._history_agent_tasks[task.get_name()]
