#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The wake gate: Peekaboo listens locally and only wakes up when addressed.

Sits after the recognizers: Moonshine on the machine, always on, and, when
there is one, a cloud recognizer behind it, connected only while awake.
Asleep, every local transcript is checked for the wake phrase and dropped
otherwise: nothing leaves the machine. A transcript that starts with the
phrase wakes the gate: the words after it go through right away. What
follows while awake comes from the cloud recognizer if there is one, with
local transcripts ignored until the gate sleeps again; with Moonshine alone
(``local_conversation``) the local transcripts are the conversation and go
through as they are, the phrase stripped if it is said again. A bare
"Peekaboo" gets a "Yes?" unless the question follows within a couple of
seconds. The gate sleeps after a quiet spell following the last word.

Small local models mangle the word on its own ("P. K.", "Pika Bu", "Peek-a-",
"Hey Pico"), so the match is phonetic rather than literal.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Optional

from loguru import logger
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Awake this long after the bot's last word or the user's last utterance.
AWAKE_WINDOW_SECS = 15.0

# A bare wake word gets "Yes?" unless the question arrives within this.
ACK_DELAY_SECS = 2.0

# Words allowed before the phrase.
_LEAD_IN = re.compile(r"^(?:\W*(?:hey|hi|ok|okay)\b)?", re.IGNORECASE)

# The phrase, phonetically: "peekaboo", "pikaboo", "peek-a-boo", "pikavu"...
_PHONETIC_KEY = "pikabu"
# Fragments the recognizers leave when the word stood alone.
_FRAGMENTS = {"pk", "pika", "piku", "pikab", "pikabu", "pikuf", "pikub"}


@dataclass
class LocalTranscriptionFrame(TranscriptionFrame):
    """A transcript from the local recognizer, as opposed to the cloud one."""


def _phonetic(word: str) -> str:
    key = re.sub(r"[^a-z]", "", word.lower())
    for pattern, sound in (("ee|ea|ie|y|i", "i"), ("oo|ou|u|o", "u"), ("c|k|q|g", "k"), ("v|d|b", "b")):
        key = re.sub(pattern, sound, key)
    return re.sub(r"(.)\1+", r"\1", key)


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _is_wake(candidate: str) -> bool:
    key = _phonetic(candidate)
    if key in _FRAGMENTS or _distance(key, _PHONETIC_KEY) <= 1:
        return True
    # "Pickle-boom": an "l" slipped in after the "k" and the "boo" grew an
    # "m". Without those it is the word; "pickle", "pickled" and "pickup"
    # stay two edits away.
    loose = re.sub(r"l", "", key).rstrip("m")
    if loose != key and _distance(loose, _PHONETIC_KEY) <= 1:
        return True
    # "Peekable", "Peekabull": the start is right and the end trails off.
    return key.startswith("pikab") and len(key) <= 8


def strip_wake(text: str) -> Optional[str]:
    """The words after the wake phrase, or None if the text does not start
    with it. An empty string means the phrase alone.

    The phrase may come out as one to three words ("Peek a boo", "Pika Bu"),
    so the leading three, two and one words are each tried as the phrase,
    longest first.
    """
    rest = _LEAD_IN.sub("", text or "", count=1)
    words = re.findall(r"[A-Za-z][A-Za-z'.-]*", rest)
    for n in (3, 2, 1):
        if len(words) < n:
            continue
        if _is_wake("".join(words[:n])):
            match = re.match(r"\W*" + r"\W*".join(map(re.escape, words[:n])) + r"[\s,.!?:;-]*", rest)
            return rest[match.end() :].strip() if match else ""
    return None


class WakeGate(FrameProcessor):
    """Passes the user's words through only while awake, or when they follow
    the wake phrase.

    Args:
        on_wake: Called when the gate wakes; connect the cloud recognizer.
        on_sleep: Called when it goes back to sleep; disconnect it.
        on_acknowledge: Called when the phrase came alone and nothing
            followed, to answer "Yes?".
        local_conversation: There is no cloud recognizer: while awake the
            local transcripts are the conversation and pass through.
    """

    def __init__(
        self,
        *,
        on_wake: Optional[Callable[[], Awaitable[None]]] = None,
        on_sleep: Optional[Callable[[], Awaitable[None]]] = None,
        on_acknowledge: Optional[Callable[[], Awaitable[None]]] = None,
        window_secs: float = AWAKE_WINDOW_SECS,
        local_conversation: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._local_conversation = local_conversation
        self._on_wake = on_wake
        self._on_sleep = on_sleep
        self._on_acknowledge = on_acknowledge
        self._window = window_secs
        self._awake_until: Optional[float] = None
        self._monitor: Optional[asyncio.Task] = None
        self._ack: Optional[asyncio.Task] = None
        self._register_event_handler("on_state")

    @property
    def awake(self) -> bool:
        return self._awake_until is not None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LocalTranscriptionFrame):
            await self._handle_local(frame, direction)
            return

        if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            # From the cloud recognizer: only ever connected while awake.
            if not self.awake:
                logger.debug(f"{self}: dropping cloud transcript while asleep: {frame.text!r}")
                return
            self._extend()
            if isinstance(frame, TranscriptionFrame):
                self._cancel_ack()
                # "Peekaboo, what time is it" while awake: the name is not
                # part of the question, and the name alone is nothing.
                remainder = strip_wake(frame.text)
                if remainder is not None:
                    if not remainder.strip():
                        logger.debug(f"{self}: the wake word alone while awake, ignored")
                        return
                    frame = replace(frame, text=remainder)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (BotStoppedSpeakingFrame, UserStoppedSpeakingFrame)) and self.awake:
            self._extend()

        await self.push_frame(frame, direction)

    async def _handle_local(self, frame: LocalTranscriptionFrame, direction: FrameDirection):
        if self.awake:
            self._extend()
            if not self._local_conversation:
                # The cloud recognizer has the conversation now.
                logger.debug(f"{self}: ignoring local transcript while awake: {frame.text!r}")
                return
            self._cancel_ack()
            text = frame.text
            remainder = strip_wake(text)
            if remainder is not None:
                if not remainder.strip():
                    logger.debug(f"{self}: the wake word alone while awake, ignored")
                    return
                text = remainder
            await self.push_frame(TranscriptionFrame(**_fields(replace(frame, text=text))), direction)
            return

        remainder = strip_wake(frame.text)
        if remainder is None:
            logger.debug(f"{self}: not for me: {frame.text!r}")
            return

        await self._wake()
        if len(remainder.split()) < 2:
            # "Peekaboo, go." is the recognizer trailing off, not a request.
            remainder = ""
        if remainder:
            logger.info(f"{self}: woke with: {remainder!r}")
            await self.push_frame(TranscriptionFrame(**_fields(replace(frame, text=remainder))), direction)
        else:
            logger.info(f"{self}: woke ({frame.text!r}); waiting for the question")
            self._cancel_ack()
            self._ack = self.create_task(self._acknowledge_later(), name="wake-ack")

    async def _acknowledge_later(self):
        await asyncio.sleep(ACK_DELAY_SECS)
        self._ack = None
        if self.awake and self._on_acknowledge:
            await self._on_acknowledge()

    def _cancel_ack(self):
        if self._ack:
            self._ack.cancel()
            self._ack = None

    async def _wake(self):
        first = not self.awake
        self._extend()
        if not first:
            return
        if self._on_wake:
            await self._on_wake()
        await self._call_event_handler("on_state", "awake")
        if self._monitor is None:
            self._monitor = self.create_task(self._watch_window(), name="wake-window")

    async def sleep(self):
        """Go to sleep now, whatever the window: the microphone was muted."""
        await self._sleep()

    async def _sleep(self):
        if not self.awake:
            return
        self._awake_until = None
        self._cancel_ack()
        if self._on_sleep:
            await self._on_sleep()
        logger.info(f"{self}: back to sleep")
        await self._call_event_handler("on_state", "asleep")

    def _extend(self):
        self._awake_until = time.monotonic() + self._window

    async def _watch_window(self):
        try:
            while True:
                await asyncio.sleep(1.0)
                if self.awake and time.monotonic() >= self._awake_until:
                    await self._sleep()
        finally:
            self._monitor = None

    async def cleanup(self):
        self._cancel_ack()
        if self._monitor:
            task, self._monitor = self._monitor, None
            await self.cancel_task(task)
        await super().cleanup()


def _fields(frame: TranscriptionFrame) -> dict:
    return {
        "text": frame.text,
        "user_id": frame.user_id,
        "timestamp": frame.timestamp,
        "language": frame.language,
        "result": frame.result,
        "finalized": frame.finalized,
    }
