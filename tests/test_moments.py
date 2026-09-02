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
