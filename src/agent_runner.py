#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio

from pipecat.pipeline.runner import PipelineRunner

from base_agent import BaseAgent


class AgentRunner:
    def __init__(self, handle_sigint: bool = True):
        self._runner = PipelineRunner(handle_sigint=handle_sigint)

    async def run(self, *args):
        for agent in args:
            if not isinstance(agent, BaseAgent):
                raise TypeError(f"Not a a valid agent: {agent}")

        coros = [self._runner.run(await t.create_task()) for t in args]
        await asyncio.gather(*coros)
