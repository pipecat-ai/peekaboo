#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moments import Moment, MomentKind, MomentPolicy  # noqa: E402


class Harness:
    def __init__(self, *, idle=True, quiet=False):
        self.idle = idle
        self.quiet = quiet
        self.spoken: list[Moment] = []
        self.bannered: list[Moment] = []
        self.policy = MomentPolicy(
            is_idle=lambda: self.idle,
            speak=self._speak,
            banner=self._banner,
            quiet_checks=[lambda: self.quiet],
            settle_secs=0.0,
        )

    async def _speak(self, moment):
        self.spoken.append(moment)

    async def _banner(self, moment):
        self.bannered.append(moment)


def test_delivers_by_priority_then_age():
    h = Harness()
    h.policy.enqueue(Moment(kind=MomentKind.WATCH, text="watch"))
    h.policy.enqueue(Moment(kind=MomentKind.MEETING, prompt="meeting"))
    h.policy.enqueue(Moment(kind=MomentKind.ANSWER, text="answer"))

    async def run():
        for _ in range(3):
            await h.policy.step()

    asyncio.run(run())
    assert [m.kind for m in h.spoken] == [
        MomentKind.ANSWER,
        MomentKind.MEETING,
        MomentKind.WATCH,
    ]
    assert h.policy.last_meeting is not None and h.policy.last_meeting.prompt == "meeting"


def test_nothing_while_someone_is_talking():
    h = Harness(idle=False)
    h.policy.enqueue(Moment(kind=MomentKind.ANSWER, text="answer"))
    asyncio.run(h.policy.step())
    assert h.spoken == []
    h.idle = True
    asyncio.run(h.policy.step())
    assert [m.text for m in h.spoken] == ["answer"]


def test_quiet_rule_banners_unsolicited_but_speaks_answers():
    h = Harness(quiet=True)
    h.policy.enqueue(Moment(kind=MomentKind.WATCH, text="build done"))
    h.policy.enqueue(Moment(kind=MomentKind.ANSWER, text="answer"))

    async def run():
        await h.policy.step()
        await h.policy.step()

    asyncio.run(run())
    assert [m.text for m in h.spoken] == ["answer"]
    assert [m.text for m in h.bannered] == ["build done"]
    assert len(h.policy.pending) == 1

    # The quiet rule lifts: the held moment is spoken, and not bannered again.
    h.quiet = False
    asyncio.run(h.policy.step())
    assert [m.text for m in h.spoken] == ["answer", "build done"]
    assert len(h.bannered) == 1
    assert h.policy.pending == []


def test_snooze_and_expiry():
    h = Harness()
    meeting = Moment(kind=MomentKind.MEETING, prompt="standup", url="https://zoom.us/j/1")
    h.policy.enqueue(meeting)
    asyncio.run(h.policy.step())
    assert h.spoken == [meeting]

    h.policy.snooze(meeting, minutes=10)
    asyncio.run(h.policy.step())
    assert len(h.spoken) == 1  # not before ten minutes
    assert h.policy.pending[0].url == "https://zoom.us/j/1"

    expired = Moment(kind=MomentKind.WATCH, text="old", expires_at=0.0)
    h.policy.enqueue(expired)
    asyncio.run(h.policy.step())
    assert expired not in h.policy.pending


def test_answers_skip_the_settle_wait_but_unsolicited_moments_do_not():
    spoken: list[Moment] = []

    async def speak(m):
        spoken.append(m)

    # A long settle: the conversation just went idle, nothing has settled yet.
    policy = MomentPolicy(is_idle=lambda: True, speak=speak, settle_secs=10.0)
    policy.enqueue(Moment(kind=MomentKind.WATCH, text="watch"))
    policy.enqueue(Moment(kind=MomentKind.ANSWER, text="answer"))

    async def run():
        await policy.step()
        await policy.step()

    asyncio.run(run())
    assert [m.text for m in spoken] == ["answer"]
    assert [m.text for m in policy.pending] == ["watch"]


def test_strip_wake_recognizes_the_phrase_and_its_mishearings():
    from processors.wake import strip_wake

    assert strip_wake("Peekaboo, what does the terminal say?") == "what does the terminal say?"
    assert strip_wake("hey peekaboo what time is it") == "what time is it"
    assert strip_wake("Peek a boo.") == ""
    assert strip_wake("pekaboo tell me when the build finishes") == "tell me when the build finishes"
    # How Moonshine actually heard it, live and on synthesized clips.
    for heard, rest in [
        ("P. K.", ""),
        ("Hey, Pico,", ""),
        ("Peek-a-", ""),
        ("Pikaboo.", ""),
        ("Hey Pikaboo, what does the terminal say?", "what does the terminal say?"),
        ("Hey Pika Bu, what does the terminal say?", "what does the terminal say?"),
        ("Hey Pikavu, what does the terminal say?", "what does the terminal say?"),
        ("Peek-a-doo. What was I doing this morning?", "What was I doing this morning?"),
        ("Pika Boo, what was I doing this morning?", "what was I doing this morning?"),
        ("Peak of.", ""),
        ("Peekable,", ""),
        ("Hey, Peekaboo,, show me the timeline.", "show me the timeline."),
        ("Pick a book,", ""),
    ]:
        assert strip_wake(heard) == rest, heard
    # Everyday words that sound nothing like it stay asleep.
    for other in ["what does the terminal say?", "", "Pick one.", "Peak hours are busy.", "Yeah What was I doing this morning?", "Thank you.", "P.", "Papi Kaboo, what does the terminal say?"]:
        assert strip_wake(other) is None, other


def test_store_keeps_asked_questions(tmp_path):
    import asyncio

    from store.sqlite_store import SQLiteStore

    async def go():
        store = SQLiteStore(root=tmp_path)
        await store.open()
        try:
            ask = await store.add_ask("what was I doing?", "You were in the terminal.", "voice", [3, 1])
            assert ask.id and ask.observation_ids == [3, 1]
            asks = await store.asks()
            assert [a.question for a in asks] == ["what was I doing?"]
            assert (await store.get_ask(ask.id)).source == "voice"
            await store.delete_ask(ask.id)
            assert await store.asks() == []
        finally:
            await store.close()

    asyncio.run(go())


def test_store_finds_past_questions_by_words(tmp_path):
    import asyncio

    from store.sqlite_store import SQLiteStore

    async def go():
        store = SQLiteStore(root=tmp_path)
        await store.open()
        try:
            await store.add_ask("what PR did I look at", "Pipecat PR 4540 about Deepgram.", "typed", [])
            await store.add_ask("what was I doing this morning", "You were in the terminal.", "voice", [])
            assert [a.question for a in await store.search_asks("pull deepgram")] == []
            assert [a.question for a in await store.search_asks("deepgram")] == ["what PR did I look at"]
            assert len(await store.search_asks("")) == 2
        finally:
            await store.close()

    asyncio.run(go())
