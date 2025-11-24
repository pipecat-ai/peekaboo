#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from base_store import BaseStore, ImageBatch, ImageRecord


class PeekabooFileStore(BaseStore):
    def __init__(self, *, store_path: Path) -> None:
        self._store_path = store_path
        self._locks: Dict[Path, asyncio.Lock] = {}

    async def append(self, record: ImageRecord):
        batch = await self.load(record.datetime)

        if not batch:
            batch = ImageBatch(images=[])

        batch.images.append(record)

        await self._save(batch, record.datetime)

    async def load(self, date: datetime) -> Optional[ImageBatch]:
        file_path = self._file_path(date)

        lock = self._get_lock(file_path)

        async with lock:
            if not file_path.exists():
                return None

            data = json.loads(file_path.read_text())
            return ImageBatch(**data)

    async def _save(self, batch: ImageBatch, date: datetime):
        file_path = self._file_path(date)
        lock = self._get_lock(file_path)

        async with lock:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(batch.model_dump_json(indent=2))

    def _get_lock(self, path: Path) -> asyncio.Lock:
        if path not in self._locks:
            self._locks[path] = asyncio.Lock()
        return self._locks[path]

    def _file_path(self, date: datetime) -> Path:
        # year/month/peekaboo-YYYY-MM-DD-HH.json
        year = date.strftime("%Y")
        month = date.strftime("%m")
        filename = date.strftime("peekaboo-%Y-%m-%d-%H.json")
        return self._store_path / year / month / filename
