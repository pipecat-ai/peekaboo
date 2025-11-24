#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel


class ImageRecord(BaseModel):
    type: Literal["description", "watchlist"]
    content: str
    timestamp: int

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)

class ImageCollection(BaseModel):
    images: List[ImageRecord]

class ImageBatch(BaseModel):
    images: List[ImageRecord]
    index: int
    total: int

class BaseStore(ABC):

    @abstractmethod
    async def append(self, record: ImageRecord):
        pass

    @abstractmethod
    async def load(self, date: datetime, batch_index: int) -> Optional[ImageBatch]:
        pass
