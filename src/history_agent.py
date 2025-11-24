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

You have access to historical screen information. Use the [load_history] tool to
load information with the timestamp you consider necessary.

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
        llm.register_function("load_history", self._load_history)

        history_function = FunctionSchema(
            name="load_history",
            description="Call this function when you need to load image descriptions.",
            properties={
                "timestamp": {
                    "type": "string",
                    "description": "A timestamp in this format: Nov 21, 2025 13:54",
                }
            },
            required=["timestamp"],
        )

        tools = ToolsSchema(standard_tools=[history_function])

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

    async def _load_history(self, params: FunctionCallParams):
        timestamp = params.arguments["timestamp"]
        date = datetime.strptime(timestamp, "%b %d, %Y %H:%M")

        logger.debug(f"Loading historical data from {date}")

        batch = await self._store.load(date)
        if batch and batch.images:
            batch_dict = batch.model_dump()
            await params.result_callback(batch_dict["images"])
        else:
            await params.result_callback("There's no information from the given date.")
