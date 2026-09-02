#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Import the old hourly JSON files into the SQLite store.

Usage: uv run src/store/migrate.py db

Reads every ``<root>/YYYY/MM/DD/peekaboo-*.json`` and inserts its records as
observations. Records already present (same timestamp and content) are
skipped, so the import can be run again safely. The JSON files are left alone.
"""

import asyncio
import json
import sys
from pathlib import Path

from store.models import Observation
from store.sqlite_store import SQLiteStore


async def import_json(root: Path) -> tuple[int, int]:
    store = SQLiteStore(root=root)
    await store.open()
    imported = skipped = 0
    try:
        for path in sorted(root.glob("*/*/*/peekaboo-*.json")):
            data = json.loads(path.read_text())
            for record in data.get("images", []):
                timestamp = int(record["timestamp"])
                content = record.get("content", "")
                existing = await store.timeline(
                    since=Observation(timestamp=timestamp, content="").datetime,
                    until=Observation(timestamp=timestamp, content="").datetime,
                    limit=50,
                )
                if any(o.content == content for o in existing):
                    skipped += 1
                    continue
                kind = record.get("type", "description")
                await store.add(
                    Observation(
                        timestamp=timestamp,
                        kind=kind if kind in ("description", "watchlist") else "description",
                        content=content,
                    )
                )
                imported += 1
    finally:
        await store.close()
    return imported, skipped


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    done, dup = asyncio.run(import_json(Path(sys.argv[1])))
    print(f"imported {done} observations, skipped {dup} already present")
