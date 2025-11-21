#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List, Literal

from pydantic import BaseModel


class ImageRecord(BaseModel):
    type: Literal["description", "watchlist"]
    content: str
    timestamp: int

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)

class ImageBatch(BaseModel):
    images: List[ImageRecord]

class BaseStore(ABC):

    @abstractmethod
    async def append(self, record: ImageRecord):
        pass

    @abstractmethod
    async def load(self, date: datetime) -> ImageBatch:
        pass
