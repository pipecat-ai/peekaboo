#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import json
from datetime import datetime, date
from math import ceil
from pathlib import Path
from typing import Dict, Optional, Sequence

from base_store import BaseStore, ImageBatch, ImageCollection, ImageRecord

BATCH_IMAGE_SIZE = 25


class PeekabooFileStore(BaseStore):
    def __init__(self, *, store_path: Path) -> None:
        self._store_path = store_path
        self._locks: Dict[Path, asyncio.Lock] = {}

    async def append(self, record: ImageRecord):
        collection = await self._load(record.datetime)

        if not collection:
            collection = ImageCollection(images=[])

        collection.images.append(record)

        await self._save(collection, record.datetime)

    async def available(self, date: date) -> Sequence[int]:
        date_path = self._date_path(date)

        if not date_path.exists():
            return []

        hours = []
        for file in date_path.iterdir():
            # Match pattern exactly: peekaboo-YYYY-MM-DD-HH.json
            hour = int(file.stem[-2:])
            hours.append(hour)

        return sorted(hours)

    async def load(self, date: datetime, batch_index: int) -> Optional[ImageBatch]:
        collection = await self._load(date)

        if not collection:
            return None

        collection_size = len(collection.images)

        first = batch_index * BATCH_IMAGE_SIZE
        last = min(first + BATCH_IMAGE_SIZE, collection_size) - 1
        batch_total = int(ceil(collection_size / BATCH_IMAGE_SIZE))

        return ImageBatch(
            images=collection.images[first:last],
            index=batch_index,
            total=batch_total,
        )

    async def _load(self, date: datetime) -> Optional[ImageCollection]:
        file_path = self._datetime_file_path(date)

        lock = self._get_lock(file_path)

        async with lock:
            if not file_path.exists():
                return None

            data = json.loads(file_path.read_text())
            return ImageCollection(**data)

    async def _save(self, collection: ImageCollection, date: datetime):
        file_path = self._datetime_file_path(date)
        lock = self._get_lock(file_path)

        async with lock:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(collection.model_dump_json(indent=2))

    def _get_lock(self, path: Path) -> asyncio.Lock:
        if path not in self._locks:
            self._locks[path] = asyncio.Lock()
        return self._locks[path]

    def _date_path(self, date: date) -> Path:
        # year/month/day/peekaboo-YYYY-MM-DD-HH.json
        year = date.strftime("%Y")
        month = date.strftime("%m")
        day = date.strftime("%d")
        return self._store_path / year / month / day

    def _datetime_file_path(self, date: datetime) -> Path:
        # year/month/day/peekaboo-YYYY-MM-DD-HH.json
        year = date.strftime("%Y")
        month = date.strftime("%m")
        day = date.strftime("%d")
        filename = date.strftime("peekaboo-%Y-%m-%d-%H.json")
        return self._store_path / year / month / day / filename
