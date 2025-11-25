#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os
from datetime import datetime

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.producer_processor import ProducerProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.llm_service import FunctionCallParams

from base_agent import BaseAgent
from base_store import BaseStore
from processors.history import HistoryContextProcessor

today = datetime.now().astimezone().strftime("%B %d, %Y %Z")

SYSTEM_INSTRUCTION = f"""

You are an assistant to a vision agent. Today is {today}.

You can access historical screen information using two tools:
[available_history] and [load_history].

[available_history] rules:

- Use this tool to check which hours of a specific day have available data.

- You may specify only one day per call.

- The date must be in this exact format: Nov 21, 2025.

- The tool returns a list of integers representing hours in 24-hour format
  (e.g., 0 = 12:00 AM, 13 = 1:00 PM).


[load_history] rules:

- Use this tool to retrieve the actual historical screen information.

- You may specify only one hour per call.

- The timestamp must be in this exact format: Nov 21, 2025 13:00.

- Only load information for hours that are confirmed available via
  [available_history].

- The tool may return incomplete information. Use batch_index to load additional
  batches.

- Always start with batch_index = 0 and load batches sequentially in order (0,
  then 1, then 2, etc.).

Be extremely brief. All responses are spoken aloud. Avoid emojis, bullet points,
or anything difficult to vocalize.

"""


class HistoryAgent(BaseAgent):
    def __init__(
        self,
        *,
        id: str,
        query: str,
        store: BaseStore,
        response_processor: ProducerProcessor,
        max_tokens: int = 16000,
        thinking_budget_tokens: int = 10000,
    ):
        self._id = id
        self._query = query
        self._store = store
        self._response_processor = response_processor
        self._max_tokens = max_tokens
        self._thinking_budget_tokens = thinking_budget_tokens

    async def create_task(self) -> PipelineTask:
        llm = AnthropicLLMService(
            name="HistoryAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            params=AnthropicLLMService.InputParams(
                max_tokens=self._max_tokens,
                extra={
                    "thinking": {"type": "enabled", "budget_tokens": self._thinking_budget_tokens},
                },
            ),
        )
        llm.register_function("available_history", self._available_history)
        llm.register_function("load_history", self._load_history)

        available_function = FunctionSchema(
            name="available_history",
            description="Use this function to find which hours have historical data available for a specific day.",
            properties={
                "date": {
                    "type": "string",
                    "description": "The date to check, in this exact format: Nov 21, 2025.",
                },
            },
            required=["date"],
        )

        load_function = FunctionSchema(
            name="load_history",
            description="Use this function to load image descriptions for a specific hour.",
            properties={
                "timestamp": {
                    "type": "string",
                    "description": "The timestamp to load, in this exact format: Nov 21, 2025 13:54.",
                },
                "batch_index": {
                    "type": "integer",
                    "description": "The index of the image batch to load for that hour.",
                },
            },
            required=["timestamp", "batch_index"],
        )

        tools = ToolsSchema(standard_tools=[available_function, load_function])

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            },
        ]

        context = LLMContext(messages, tools)
        context_aggregator = LLMContextAggregatorPair(context)

        context_processor = HistoryContextProcessor(response_processor=self._response_processor)

        pipeline = Pipeline(
            [
                context_aggregator.user(),  # User spoken responses
                llm,  # LLM
                context_aggregator.assistant(),  # Assistant spoken responses and tool context
                context_processor,
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

        await task.queue_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": self._query}], run_llm=True
            )
        )

        return task

    async def _available_history(self, params: FunctionCallParams):
        date_str = params.arguments["date"]

        date = datetime.strptime(date_str, "%b %d, %Y")

        logger.debug(f"Loading available historical data from {date_str}")

        available = await self._store.available(date)

        await params.result_callback(available)

    async def _load_history(self, params: FunctionCallParams):
        timestamp = params.arguments["timestamp"]
        batch_index = params.arguments["batch_index"]

        date = datetime.strptime(timestamp, "%b %d, %Y %H:%M")

        logger.debug(f"Loading historical data from {date}")

        batch = await self._store.load(date, batch_index)
        if batch and batch.images:
            batch_dict = batch.model_dump()
            images = batch_dict["images"]

            result = f"Image batch {batch.index} out of {batch.total}\n\n{images}"

            await params.result_callback(result)
        else:
            await params.result_callback(f"There's no information from {timestamp}.")
