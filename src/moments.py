#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Moments: things the assistant says without being asked, and when it may.

A moment is queued by whoever has something to say, a watch hit, a meeting
reminder, the answer to an earlier question, and the policy decides when it
goes out. Nothing is spoken while the user or the bot is talking, and
unsolicited moments stay quiet while any quiet rule holds, showing as a
banner instead and speaking once the rule lifts.
"""

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional

from loguru import logger


class MomentKind(StrEnum):
    ANSWER = "answer"
    """The answer to something the user asked. Expected, never held quiet."""
    MEETING = "meeting"
    WATCH = "watch"
    WARNING = "warning"


PRIORITY = {
    MomentKind.ANSWER: 0,
    MomentKind.MEETING: 1,
    MomentKind.WATCH: 2,
    MomentKind.WARNING: 3,
}

_ids = itertools.count(1)


@dataclass
class Moment:
    kind: MomentKind
    text: Optional[str] = None
    """Spoken verbatim."""
    prompt: Optional[str] = None
    """Given to the LLM as a developer instruction, so it phrases the moment."""
    url: Optional[str] = None
    """A link the moment carries, such as a meeting's join link."""
    not_before: float = 0.0
    """Monotonic time before which the moment waits (snooze)."""
    expires_at: Optional[float] = None
    """Monotonic time after which the moment is dropped unspoken."""
    id: int = field(default_factory=lambda: next(_ids))
    created: float = field(default_factory=time.monotonic)
    bannered: bool = False

    @property
    def unsolicited(self) -> bool:
        return self.kind != MomentKind.ANSWER


class MomentPolicy:
    """Decides when queued moments are delivered.

    Args:
        is_idle: Whether nobody is talking right now.
        speak: Delivers a moment by voice.
        banner: Delivers a moment silently, for when speaking must wait.
        quiet_checks: Rules that hold unsolicited moments: another app has
            the microphone, the screen is being shared. Any one holding is
            enough.
        settle_secs: How long the conversation must have been idle before a
            moment goes out, so it never lands on the user's next word.
    """

    def __init__(
        self,
        *,
        is_idle: Callable[[], bool],
        speak: Callable[[Moment], Awaitable[None]],
        banner: Optional[Callable[[Moment], Awaitable[None]]] = None,
        quiet_checks: Optional[list[Callable[[], bool]]] = None,
        settle_secs: float = 0.7,
        tick_secs: float = 0.25,
    ):
        self._is_idle = is_idle
        self._speak = speak
        self._banner = banner
        self._quiet_checks = list(quiet_checks or [])
        self._settle_secs = settle_secs
        self._tick_secs = tick_secs
        self._queue: list[Moment] = []
        self._idle_since: Optional[float] = None
        self._last_meeting: Optional[Moment] = None

    @property
    def pending(self) -> list[Moment]:
        return list(self._queue)

    @property
    def last_meeting(self) -> Optional[Moment]:
        """The meeting reminder most recently delivered, for join and snooze."""
        return self._last_meeting

    def enqueue(self, moment: Moment):
        logger.debug(f"moment queued: {moment.kind} #{moment.id}")
        self._queue.append(moment)

    def snooze(self, moment: Moment, minutes: float):
        """Deliver a moment again after a delay."""
        again = Moment(
            kind=moment.kind,
            text=moment.text,
            prompt=moment.prompt,
            url=moment.url,
            not_before=time.monotonic() + minutes * 60,
            expires_at=moment.expires_at,
        )
        self.enqueue(again)

    def quiet(self) -> bool:
        return any(check() for check in self._quiet_checks)

    async def run(self):
        """Deliver moments as the conversation allows. Runs until cancelled."""
        while True:
            await asyncio.sleep(self._tick_secs)
            try:
                await self.step()
            except Exception as e:  # noqa: BLE001 - one bad moment must not stop the rest
                logger.warning(f"moment delivery failed: {e}")

    async def step(self):
        """One pass: drop expired moments, deliver at most one."""
        now = time.monotonic()
        self._queue = [m for m in self._queue if m.expires_at is None or m.expires_at > now]

        if not self._is_idle():
            self._idle_since = None
            return
        if self._idle_since is None:
            self._idle_since = now
        if now - self._idle_since < self._settle_secs:
            return

        ready = [m for m in self._queue if m.not_before <= now]
        if not ready:
            return
        ready.sort(key=lambda m: (PRIORITY[m.kind], m.created))

        quiet = self.quiet()
        for moment in ready:
            if moment.unsolicited and quiet:
                if not moment.bannered and self._banner:
                    moment.bannered = True
                    await self._banner(moment)
                continue
            self._queue.remove(moment)
            if moment.kind == MomentKind.MEETING:
                self._last_meeting = moment
            logger.debug(f"moment delivered: {moment.kind} #{moment.id}")
            await self._speak(moment)
            # The bot is about to talk; wait for the conversation to settle
            # again before the next one.
            self._idle_since = None
            return
