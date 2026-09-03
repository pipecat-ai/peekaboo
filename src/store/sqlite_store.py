#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import re
import sqlite3
from dataclasses import dataclass
import time
import json
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
SCHEMA_VERSION = 2
IMAGE_RETENTION_DAYS = 7
# Screen stills are context for their moment; they go sooner than window frames.
STILL_RETENTION_DAYS = 2

# Content rows: window frames, and screen descriptions recorded before windows
# were (they have no moment). Screen stills and screen descriptions taken as
# part of a moment are context, kept for the stage and the scrubber.
CONTENT = "NOT (target = 'screen' AND moment IS NOT NULL)"
CONTENT_O = "NOT (o.target = 'screen' AND o.moment IS NOT NULL)"

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
    thumbnail_path TEXT,
    moment INTEGER,
    rect TEXT
);
CREATE INDEX IF NOT EXISTS observations_ts ON observations(ts);
CREATE INDEX IF NOT EXISTS observations_hash ON observations(frame_hash);

CREATE TABLE IF NOT EXISTS asks (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'typed',
    observation_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS asks_ts ON asks(ts);

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
        moment=row["moment"],
        rect=json.loads(row["rect"]) if row["rect"] else None,
    )


@dataclass
class Ask:
    """A question that was asked, what was answered, and the frames behind it."""

    id: int
    ts: int
    question: str
    answer: str
    source: str  # "typed" or "voice"
    observation_ids: list[int]


def _row_to_ask(row: sqlite3.Row) -> Ask:
    return Ask(
        id=row["id"],
        ts=row["ts"],
        question=row["question"],
        answer=row["answer"],
        source=row["source"],
        observation_ids=[int(i) for i in json.loads(row["observation_ids"] or "[]")],
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
        # Columns added after the first release, for stores created before.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(observations)")}
        for column, decl in (("moment", "INTEGER"), ("rect", "TEXT")):
            if column not in have:
                conn.execute(f"ALTER TABLE observations ADD COLUMN {column} {decl}")
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
                 frame_hash, screenshot_path, thumbnail_path, moment, rect)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                o.moment,
                json.dumps(o.rect) if o.rect else None,
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

    async def prune_images(
        self, older_than_days: int = IMAGE_RETENTION_DAYS, stills_older_than_days: int = STILL_RETENTION_DAYS
    ) -> int:
        """Delete frames older than the retention window, screen stills
        sooner. Text is kept.

        Returns how many observations lost their images.
        """
        cutoff = int((datetime.now() - timedelta(days=older_than_days)).timestamp())
        stills_cutoff = int((datetime.now() - timedelta(days=stills_older_than_days)).timestamp())
        pruned = await self._run(self._prune_images_sync, cutoff, "")
        pruned += await self._run(self._prune_images_sync, stills_cutoff, " AND kind = 'screen'")
        return pruned

    def _prune_images_sync(self, cutoff: int, extra: str) -> int:
        rows = self._db.execute(
            "SELECT id, screenshot_path, thumbnail_path FROM observations "
            f"WHERE ts < ? AND screenshot_path IS NOT NULL{extra}",
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
            f"WHERE ts < ? AND screenshot_path IS NOT NULL{extra}",
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

    #
    # Questions asked and their answers: each one cost a search and a model
    # call, so they are kept to be looked at again.
    #

    async def add_ask(self, question: str, answer: str, source: str, observation_ids: Sequence[int]) -> Ask:
        return await self._run(self._add_ask_sync, question, answer, source, list(observation_ids))

    def _add_ask_sync(self, question: str, answer: str, source: str, ids: list[int]) -> Ask:
        ts = int(time.time())
        cur = self._db.execute(
            "INSERT INTO asks (ts, question, answer, source, observation_ids) VALUES (?, ?, ?, ?, ?)",
            (ts, question, answer, source, json.dumps(ids)),
        )
        self._db.commit()
        return Ask(id=cur.lastrowid, ts=ts, question=question, answer=answer, source=source, observation_ids=ids)

    async def asks(self, limit: int = 100) -> list[Ask]:
        """The newest questions, newest first."""
        return await self._run(self._asks_sync, limit)

    def _asks_sync(self, limit: int) -> list[Ask]:
        rows = self._db.execute("SELECT * FROM asks ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_ask(r) for r in rows]

    async def search_asks(self, query: str, limit: int = 5) -> list[Ask]:
        """Past questions whose question or answer contains every word of
        the query, newest first. An empty query gives the newest ones."""
        return await self._run(self._search_asks_sync, query, limit)

    def _search_asks_sync(self, query: str, limit: int) -> list[Ask]:
        words = [w for w in query.lower().split() if len(w) > 2] or []
        sql = "SELECT * FROM asks"
        args: list = []
        if words:
            sql += " WHERE " + " AND ".join("instr(lower(question || ' ' || answer), ?) > 0" for _ in words)
            args = words
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        rows = self._db.execute(sql, [*args, limit]).fetchall()
        return [_row_to_ask(r) for r in rows]

    async def get_ask(self, ask_id: int) -> Optional[Ask]:
        return await self._run(self._get_ask_sync, ask_id)

    def _get_ask_sync(self, ask_id: int) -> Optional[Ask]:
        row = self._db.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        return _row_to_ask(row) if row else None

    async def delete_ask(self, ask_id: int) -> None:
        await self._run(self._delete_ask_sync, ask_id)

    def _delete_ask_sync(self, ask_id: int) -> None:
        self._db.execute("DELETE FROM asks WHERE id = ?", (ask_id,))
        self._db.commit()

    async def recent(self, limit: int = 10) -> list[Observation]:
        """The newest content observations (window frames, and screen
        descriptions from before windows were recorded), newest first."""
        return await self._run(self._recent_sync, limit)

    def _recent_sync(self, limit: int) -> list[Observation]:
        rows = self._db.execute(
            f"SELECT * FROM observations WHERE {CONTENT} ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_observation(r) for r in rows]

    async def stills(self, since: Optional[datetime] = None, until: Optional[datetime] = None, limit: int = 5000):
        """The screen stills in a time window, oldest first: the context of
        each moment, for scrubbing and the viewer's stage."""
        return await self._run(self._stills_sync, _to_ts(since, 0), _to_ts(until, 2**62), limit)

    def _stills_sync(self, since: int, until: int, limit: int) -> list[Observation]:
        rows = self._db.execute(
            "SELECT * FROM observations WHERE kind = 'screen' AND ts BETWEEN ? AND ? ORDER BY ts, id LIMIT ?",
            (since, until, limit),
        ).fetchall()
        return [_row_to_observation(r) for r in rows]

    async def last_frames(self, since_days: int = 2) -> dict[str, tuple[str, str]]:
        """Per target, the newest analysed frame's hash and description, for
        a restart to pick up where it left off instead of re-describing
        every window."""
        cutoff = int((datetime.now() - timedelta(days=since_days)).timestamp())
        return await self._run(self._last_frames_sync, cutoff)

    def _last_frames_sync(self, cutoff: int) -> dict[str, tuple[str, str]]:
        rows = self._db.execute(
            """
            SELECT target, frame_hash, content FROM observations o
            WHERE kind = 'description' AND frame_hash IS NOT NULL AND ts >= ?
              AND id = (SELECT MAX(id) FROM observations WHERE target = o.target AND kind = 'description' AND frame_hash IS NOT NULL)
            """,
            (cutoff,),
        ).fetchall()
        return {r["target"]: (r["frame_hash"], r["content"] or "") for r in rows}

    async def moment(self, moment: int) -> list[Observation]:
        """Everything captured in one recording tick: the screen still and the
        window frames analysed from it."""
        return await self._run(self._moment_sync, int(moment))

    def _moment_sync(self, moment: int) -> list[Observation]:
        rows = self._db.execute("SELECT * FROM observations WHERE moment = ? ORDER BY ts, id", (moment,)).fetchall()
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
            f"""
            SELECT o.* FROM observations_fts f
            JOIN observations o ON o.id = f.rowid
            WHERE observations_fts MATCH ? AND o.ts BETWEEN ? AND ? AND {CONTENT_O}
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
            f"SELECT * FROM observations WHERE ts BETWEEN ? AND ? AND {CONTENT} ORDER BY ts, id LIMIT ?",
            (since, until, limit),
        ).fetchall()
        return [_row_to_observation(r) for r in rows]

    async def month_counts(self, year: int, month: int) -> dict[str, int]:
        """Memories per day (ISO date) in a month, for the calendar."""
        start = datetime(year, month, 1)
        end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
        return await self._run(self._month_counts_sync, int(start.timestamp()), int(end.timestamp()))

    def _month_counts_sync(self, since: int, until: int) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS d, COUNT(*) AS n FROM observations "
            "WHERE ts >= ? AND ts < ? GROUP BY d",
            (since, until),
        ).fetchall()
        return {r["d"]: r["n"] for r in rows}

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
