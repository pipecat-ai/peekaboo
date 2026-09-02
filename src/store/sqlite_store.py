#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence

from loguru import logger
from PIL import Image

from store.images import save_frame
from store.models import DayCoverage, HourCoverage, Observation

DB_FILE = "peekaboo.db"
FRAMES_DIR = "frames"
SCHEMA_VERSION = 1
IMAGE_RETENTION_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    target TEXT NOT NULL DEFAULT 'screen',
    app TEXT,
    title TEXT,
    kind TEXT NOT NULL DEFAULT 'description',
    content TEXT NOT NULL,
    verbatim_text TEXT NOT NULL DEFAULT '',
    frame_hash TEXT,
    screenshot_path TEXT,
    thumbnail_path TEXT
);
CREATE INDEX IF NOT EXISTS observations_ts ON observations(ts);
CREATE INDEX IF NOT EXISTS observations_hash ON observations(frame_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    content,
    verbatim_text,
    content='observations',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(rowid, content, verbatim_text)
    VALUES (new.id, new.content, new.verbatim_text);
END;
CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, content, verbatim_text)
    VALUES ('delete', old.id, old.content, old.verbatim_text);
END;
CREATE TRIGGER IF NOT EXISTS observations_au
AFTER UPDATE OF content, verbatim_text ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, content, verbatim_text)
    VALUES ('delete', old.id, old.content, old.verbatim_text);
    INSERT INTO observations_fts(rowid, content, verbatim_text)
    VALUES (new.id, new.content, new.verbatim_text);
END;
"""

_TOKEN = re.compile(r"[\w][\w.\-/#@]*")


def fts_query(text: str) -> Optional[str]:
    """Turn free text into an FTS5 query: any token may match, ranked by bm25.

    Tokens are quoted so punctuation in URLs and error codes can't break the
    query syntax. Returns None when there is nothing to search for.
    """
    tokens = [t.strip(".-/") for t in _TOKEN.findall(text)]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _to_ts(moment: Optional[datetime], default: int) -> int:
    if moment is None:
        return default
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return int(moment.timestamp())


def _row_to_observation(row: sqlite3.Row) -> Observation:
    verbatim = row["verbatim_text"]
    return Observation(
        id=row["id"],
        timestamp=row["ts"],
        target=row["target"],
        app=row["app"],
        title=row["title"],
        kind=row["kind"],
        content=row["content"],
        verbatim_text=verbatim.split("\n") if verbatim else [],
        frame_hash=row["frame_hash"],
        screenshot_path=row["screenshot_path"],
        thumbnail_path=row["thumbnail_path"],
    )


class SQLiteStore:
    """Observations in SQLite with full-text search, frames as JPEGs on disk.

    Everything runs on one worker thread with one connection, so callers on
    the event loop never block on disk and SQLite never sees two threads.
    Paths stored in the database are relative to the store root.
    """

    def __init__(self, *, root: Path):
        self._root = root
        self._db_path = root / DB_FILE
        self._frames_dir = root / FRAMES_DIR
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="peekaboo-store")
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, relative_path: str) -> Path:
        """Absolute path of a stored frame."""
        return self._root / relative_path

    #
    # Lifecycle
    #

    async def open(self) -> None:
        await self._run(self._open_sync)

    async def close(self) -> None:
        await self._run(self._close_sync)
        self._executor.shutdown(wait=True)

    def _open_sync(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        self._conn = conn

    def _close_sync(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    async def _run(self, fn: Callable, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    @property
    def _db(self) -> sqlite3.Connection:
        assert self._conn is not None, "store is not open"
        return self._conn

    #
    # Writing
    #

    async def add(self, observation: Observation) -> Observation:
        """Insert an observation. Returns it with its id set."""
        return await self._run(self._add_sync, observation)

    def _add_sync(self, o: Observation) -> Observation:
        cur = self._db.execute(
            """
            INSERT INTO observations
                (ts, target, app, title, kind, content, verbatim_text,
                 frame_hash, screenshot_path, thumbnail_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                o.timestamp,
                o.target,
                o.app,
                o.title,
                o.kind,
                o.content,
                "\n".join(o.verbatim_text),
                o.frame_hash,
                o.screenshot_path,
                o.thumbnail_path,
            ),
        )
        self._db.commit()
        return o.model_copy(update={"id": cur.lastrowid})

    async def save_frame(
        self, image: Image.Image, timestamp: int, frame_hash: str
    ) -> tuple[str, str]:
        """Write a frame and thumbnail, or reuse the files of an identical frame.

        Returns (screenshot_path, thumbnail_path) relative to the root.
        """
        return await self._run(self._save_frame_sync, image, timestamp, frame_hash)

    def _save_frame_sync(self, image: Image.Image, timestamp: int, frame_hash: str):
        row = self._db.execute(
            """
            SELECT screenshot_path, thumbnail_path FROM observations
            WHERE frame_hash = ? AND screenshot_path IS NOT NULL
            ORDER BY ts DESC LIMIT 1
            """,
            (frame_hash,),
        ).fetchone()
        if row and self.resolve(row["screenshot_path"]).exists():
            return row["screenshot_path"], row["thumbnail_path"]

        day = datetime.fromtimestamp(timestamp)
        directory = self._frames_dir / day.strftime("%Y/%m/%d")
        frame_path, thumb_path = save_frame(image, directory, f"{timestamp}-{frame_hash}")
        return (
            str(frame_path.relative_to(self._root)),
            str(thumb_path.relative_to(self._root)),
        )

    async def prune_images(self, older_than_days: int = IMAGE_RETENTION_DAYS) -> int:
        """Delete frames older than the retention window. Text is kept.

        Returns how many observations lost their images.
        """
        cutoff = int((datetime.now() - timedelta(days=older_than_days)).timestamp())
        return await self._run(self._prune_images_sync, cutoff)

    def _prune_images_sync(self, cutoff: int) -> int:
        rows = self._db.execute(
            "SELECT id, screenshot_path, thumbnail_path FROM observations "
            "WHERE ts < ? AND screenshot_path IS NOT NULL",
            (cutoff,),
        ).fetchall()
        # A frame can be shared by several observations (deduplicated by hash),
        # so only delete files no observation inside the window still uses.
        for row in rows:
            for column in ("screenshot_path", "thumbnail_path"):
                path = row[column]
                if not path:
                    continue
                still_used = self._db.execute(
                    f"SELECT 1 FROM observations WHERE {column} = ? AND ts >= ? LIMIT 1",
                    (path, cutoff),
                ).fetchone()
                if not still_used:
                    self.resolve(path).unlink(missing_ok=True)
        self._db.execute(
            "UPDATE observations SET screenshot_path = NULL, thumbnail_path = NULL "
            "WHERE ts < ? AND screenshot_path IS NOT NULL",
            (cutoff,),
        )
        self._db.commit()
        if rows:
            logger.info(f"Pruned images from {len(rows)} observations older than {cutoff}")
        return len(rows)

    #
    # Reading
    #

    async def get(self, ids: Sequence[int]) -> list[Observation]:
        return await self._run(self._get_sync, list(ids))

    def _get_sync(self, ids: list[int]) -> list[Observation]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._db.execute(
            f"SELECT * FROM observations WHERE id IN ({marks}) ORDER BY ts", ids
        ).fetchall()
        return [_row_to_observation(r) for r in rows]

    async def recent(self, limit: int = 10) -> list[Observation]:
        """The newest observations, newest first."""
        return await self._run(self._recent_sync, limit)

    def _recent_sync(self, limit: int) -> list[Observation]:
        rows = self._db.execute(
            "SELECT * FROM observations ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_observation(r) for r in rows]

    async def search(
        self,
        query: str,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 20,
    ) -> list[Observation]:
        """Full-text search over descriptions and on-screen text, best first."""
        match = fts_query(query)
        if match is None:
            return []
        return await self._run(
            self._search_sync, match, _to_ts(since, 0), _to_ts(until, 2**62), limit
        )

    def _search_sync(self, match: str, since: int, until: int, limit: int):
        rows = self._db.execute(
            """
            SELECT o.* FROM observations_fts f
            JOIN observations o ON o.id = f.rowid
            WHERE observations_fts MATCH ? AND o.ts BETWEEN ? AND ?
            ORDER BY bm25(observations_fts), o.ts DESC
            LIMIT ?
            """,
            (match, since, until, limit),
        ).fetchall()
        return [_row_to_observation(r) for r in rows]

    async def timeline(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Observation]:
        """Observations in a time window, oldest first."""
        return await self._run(
            self._timeline_sync, _to_ts(since, 0), _to_ts(until, 2**62), limit
        )

    def _timeline_sync(self, since: int, until: int, limit: int):
        rows = self._db.execute(
            "SELECT * FROM observations WHERE ts BETWEEN ? AND ? ORDER BY ts, id LIMIT ?",
            (since, until, limit),
        ).fetchall()
        return [_row_to_observation(r) for r in rows]

    async def coverage(self, day: date) -> DayCoverage:
        """Which hours of a day have observations, and how many."""
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1)
        return await self._run(
            self._coverage_sync, day.isoformat(), int(start.timestamp()), int(end.timestamp())
        )

    def _coverage_sync(self, label: str, start: int, end: int) -> DayCoverage:
        rows = self._db.execute(
            "SELECT ts FROM observations WHERE ts >= ? AND ts < ?", (start, end)
        ).fetchall()
        counts: dict[int, int] = {}
        for row in rows:
            hour = datetime.fromtimestamp(row["ts"]).hour
            counts[hour] = counts.get(hour, 0) + 1
        hours = [HourCoverage(hour=h, count=c) for h, c in sorted(counts.items())]
        return DayCoverage(date=label, hours=hours)

    async def count(self) -> int:
        return await self._run(self._count_sync)

    def _count_sync(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
