#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from abc import ABC, abstractmethod

from pipecat.pipeline.task import PipelineTask


class BaseAgent(ABC):
    @abstractmethod
    async def create_task(self) -> PipelineTask:
        pass
