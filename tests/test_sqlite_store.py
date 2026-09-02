#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Store tests. Run with: uv run --with pytest pytest tests/"""

import asyncio
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from store.images import dhash, hamming  # noqa: E402
from store.migrate import import_json  # noqa: E402
from store.models import Observation  # noqa: E402
from store.sqlite_store import SQLiteStore, fts_query  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_image(color):
    return Image.new("RGB", (640, 400), color)


async def with_store(tmp_path, fn):
    store = SQLiteStore(root=tmp_path / "db")
    await store.open()
    try:
        return await fn(store)
    finally:
        await store.close()


def test_fts_query_quotes_tokens():
    assert fts_query("PR #4540 merged") == '"PR" OR "4540" OR "merged"'
    url = "github.com/pipecat-ai/pipecat/pull/4540"
    assert fts_query(url) == f'"{url}"'
    assert fts_query("   ") is None


def test_add_get_roundtrip(tmp_path):
    async def body(store):
        added = await store.add(
            Observation(
                timestamp=1_700_000_000,
                kind="watchlist",
                content="Tests passed in the terminal.",
                verbatim_text=["142 passed, 0 failed", "make test"],
                frame_hash="abcd",
            )
        )
        assert added.id is not None
        [got] = await store.get([added.id])
        assert got.content == "Tests passed in the terminal."
        assert got.verbatim_text == ["142 passed, 0 failed", "make test"]
        assert got.kind == "watchlist"
        assert got.frame_hash == "abcd"

    run(with_store(tmp_path, body))


def test_search_matches_content_and_verbatim_and_respects_window(tmp_path):
    async def body(store):
        base = int(datetime(2026, 9, 1, 9, 0).timestamp())
        a = await store.add(
            Observation(
                timestamp=base,
                content="A GitHub pull request page in Safari.",
                verbatim_text=["Rename RTVI UI Worker Protocol vocabulary #4540", "Merged"],
            )
        )
        b = await store.add(
            Observation(timestamp=base + 3600, content="A terminal running make test.")
        )
        await store.add(Observation(timestamp=base + 7200, content="Slack, a channel list."))

        hits = await store.search("pull request")
        assert [o.id for o in hits][0] == a.id

        hits = await store.search("4540")
        assert [o.id for o in hits] == [a.id]

        hits = await store.search("terminal make test")
        assert hits[0].id == b.id

        hits = await store.search(
            "terminal",
            since=datetime(2026, 9, 1, 9, 30),
            until=datetime(2026, 9, 1, 10, 30),
        )
        assert [o.id for o in hits] == [b.id]

        assert await store.search("nothing matches this") == []

    run(with_store(tmp_path, body))


def test_timeline_and_coverage(tmp_path):
    async def body(store):
        day = date(2026, 9, 1)
        for hour, n in ((9, 3), (11, 2)):
            for i in range(n):
                ts = int(datetime(2026, 9, 1, hour, i).timestamp())
                await store.add(Observation(timestamp=ts, content=f"frame {hour}:{i}"))

        cov = await store.coverage(day)
        assert cov.date == "2026-09-01"
        assert [(h.hour, h.count) for h in cov.hours] == [(9, 3), (11, 2)]

        window = await store.timeline(
            since=datetime(2026, 9, 1, 10), until=datetime(2026, 9, 1, 12)
        )
        assert [o.content for o in window] == ["frame 11:0", "frame 11:1"]

        recent = await store.recent(limit=2)
        assert [o.content for o in recent] == ["frame 11:1", "frame 11:0"]
        assert await store.count() == 5

    run(with_store(tmp_path, body))


def test_dhash_is_stable_and_distinguishes_frames():
    red = make_image((200, 30, 30))
    red_again = make_image((200, 30, 30))
    assert dhash(red) == dhash(red_again)
    striped = make_image((0, 0, 0))
    for x in range(0, 640, 40):
        for y in range(400):
            striped.putpixel((x, y), (255, 255, 255))
    assert hamming(dhash(red), dhash(striped)) > 8


def test_save_frame_dedups_by_hash_and_prunes(tmp_path):
    async def body(store):
        image = make_image((10, 20, 30))
        frame_hash = dhash(image)
        old_ts = int((datetime.now() - timedelta(days=10)).timestamp())
        new_ts = int(time.time())

        shot, thumb = await store.save_frame(image, old_ts, frame_hash)
        assert store.resolve(shot).exists() and store.resolve(thumb).exists()
        old = await store.add(
            Observation(
                timestamp=old_ts,
                content="old",
                frame_hash=frame_hash,
                screenshot_path=shot,
                thumbnail_path=thumb,
            )
        )

        # Same frame again: no new files, same paths.
        shot2, thumb2 = await store.save_frame(image, new_ts, frame_hash)
        assert (shot2, thumb2) == (shot, thumb)
        assert len(list((store.root / "frames").rglob("*.jpg"))) == 2
        new = await store.add(
            Observation(
                timestamp=new_ts,
                content="new",
                frame_hash=frame_hash,
                screenshot_path=shot2,
                thumbnail_path=thumb2,
            )
        )

        # Pruning clears the old observation's paths but keeps the files the
        # recent observation still references.
        pruned = await store.prune_images(older_than_days=7)
        assert pruned == 1
        [old_after] = await store.get([old.id])
        [new_after] = await store.get([new.id])
        assert old_after.screenshot_path is None
        assert new_after.screenshot_path == shot
        assert store.resolve(shot).exists()

        # Search still works after the update (FTS index untouched by pruning).
        assert [o.id for o in await store.search("old")] == [old.id]

    run(with_store(tmp_path, body))


def test_import_json(tmp_path):
    root = tmp_path / "db"
    day_dir = root / "2025" / "11" / "21"
    day_dir.mkdir(parents=True)
    (day_dir / "peekaboo-2025-11-21-13.json").write_text(
        json.dumps(
            {
                "images": [
                    {"type": "description", "content": "A code editor.", "timestamp": 1763730000},
                    {"type": "watchlist", "content": "Build finished.", "timestamp": 1763730060},
                ]
            }
        )
    )
    assert run(import_json(root)) == (2, 0)
    assert run(import_json(root)) == (0, 2)

    async def body(store):
        assert await store.count() == 2
        assert [o.content for o in await store.search("build")] == ["Build finished."]

    run(with_store(tmp_path, body))
