#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The ui worker: drives the Peekaboo window by voice.

A Pipecat ``UIWorker``: the page streams accessibility snapshots of itself
over RTVI, the voice worker hands over anything the user says about the
window ("open the first one", "go to the timeline", "what's the third one
about"), and this worker resolves it against the snapshot and acts through
UI commands the page executes. Its short spoken reply goes straight to the
voice worker's TTS.
"""

import asyncio
import os
import time
from datetime import datetime
from typing import Optional

from loguru import logger
from pipecat.bus.messages import BusMessage
from pipecat.bus.ui.messages import _UI_SNAPSHOT_BUS_EVENT_NAME, BusUIEventMessage
from pipecat.processors.frameworks.rtvi.models import Navigate
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.workers.llm.tool_decorator import tool
from pipecat.workers.ui.ui_worker import UIWorker

from workers.names import UI_WORKER

UI_MODEL = "claude-haiku-4-5"
UI_MAX_TOKENS = 400
# A request to the window agent is closed for it after this long.
UI_TURN_TIMEOUT_SECS = 12.0
# A snapshot older than this is not trusted for a new request; wait this long for a fresh one.
FRESH_SNAPSHOT_SECS = 2.0
SNAPSHOT_WAIT_SECS = 2.5

SCREENS = ("ask", "searches", "timeline", "watchers", "settings")

UI_INSTRUCTION = """\
You operate the Peekaboo window for a user who is speaking, not typing.
Peekaboo remembers what was on their Mac's screen; the window shows those
memories. Its screens: Ask (a question box, the answer, and grids of memory
cards), Searches (past questions), Timeline (a day by the hour), Watchers,
Settings, in sections: System (Appearance buttons System, Light, Dark; a
checkbox to start recording at launch), Audio (a "Microphone" dropdown,
System default or a device by name; an "Echo cancellation" checkbox, off for
screen recording), Recording (nothing yet): click them to change them, and a Viewer that opens
when a memory card is clicked. A memory is
a moment: the Viewer shows the screen at that moment with each captured
window outlined as a button named "<app>: <title>" (click one to read that
window; a Screen / Window toggle shows the window alone), what was on
screen, and a filmstrip of the moments around it. The header says which app
was in front ("in Ghostty"): that is focus, not what the memory is about.

Each request comes with the current <ui_state>. Memory cards are buttons
named "<app> at <time>: <what was on screen>", listed in reading order in a
grid three across. Resolve "the first one", "the third screenshot", "the
terminal one", "this" against that state.

The Timeline works by voice exactly as by mouse. It has a month calendar
(days are buttons named "Tue 2 Sep: 8 memories"), the day's hours as tracks
(hour labels are buttons named "10:00: 12 memories"; clicking one zooms into
that hour; zoomed in, the blocks on the track are buttons named
"Ghostty 10:05–10:12: 7 memories", clicking one selects it and shows its
memories in the strip, and a "Timeline" back button at the top left leaves
the zoom), a "Selected …" line, and a strip of the
selected memories (buttons named "<app> at <time>: <what was on screen>",
the selected one tagged [selected]). Clicking a day, an hour, a block, or a
strip memory does what a mouse click does. A click on a strip memory selects
it, and the selected one shows an "Open … in the Viewer" button: to open a
strip memory, [select] it, then [reply] with `click` on that Open button.
"Take a look at the third one", "open it", "click on that", "more details"
all mean open it in the Viewer. The timeline fields of [reply] do the rest:
`timeline_day` for a day ("last Tuesday", "yesterday", "the 14th": work out
the date from the current time given with the request), `timeline_hour` to
zoom into an hour, `timeline_from` and `timeline_to` to select a span of the
day. Any of them switches to the Timeline. "Show me 3 pm" is the hour;
"between ten and eleven" is a span; "this morning" is 06:00 to 12:00. Prefer
a click on a button that is in the state over the fields.

When the Viewer is open, "this image", "this screenshot", "this one" mean
the memory on the stage: answer "what is this about" from the Viewer's
"What was on screen" text and the window's name and time, in two sentences,
without clicking anything.

Questions about what the Timeline shows ("what was I working on in the last
block around three", "what is in that selection") are answered from the
state. If the block is not selected yet, call [select] with its ref first:
it clicks and returns what the window shows afterwards, with the block's
memories. Then answer with [reply] in two sentences from those memories'
names, naming the apps and the span. Do not send the user to a search.

[select] clicks, or switches screens, without answering, for when you need
to see the result before you act or speak (a block's memories, the rows of
another screen). "Open the third search" while Ask or the Viewer is showing
means: [select] with navigate "searches", then [reply] with `click` on the
third search row in the state that comes back. Never stop at the navigation
when the user asked to open, click, or look at a specific item. After
[select] you must still call [reply]; a request is not done until [reply]
is called.
Never answer in plain text. Finish every request with exactly one call to
[reply]:
- To open a memory, pass its ref as `click`. To go back, click the "Back"
  button. To switch screens, pass `navigate` with one of: ask, searches,
  timeline, watchers, settings.
- To answer a question about what is shown ("what's the third one about"),
  read it off the state and say it; do not click unless asked to open it.
- `answer` is spoken aloud as is: one short sentence, plain words, no
  markup. For an action, a few words ("Opening the terminal one."). If the
  request does not match anything on the window, say so in one sentence.
  Times in the state are for reading, not repeating: say them the way a
  person would ("ten past three in the afternoon", "from nine oh seven to
  nine twelve"), never with seconds and never as digits with dashes.
"""


class PeekabooUIWorker(UIWorker):
    """Peekaboo's window agent: Haiku over the page's accessibility snapshot,
    one ``reply`` tool that acts and speaks."""

    def __init__(self, name: str = UI_WORKER):
        llm = AnthropicLLMService(
            name="UIAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            retry_on_timeout=True,
            settings=AnthropicLLMService.Settings(
                model=UI_MODEL,
                max_tokens=UI_MAX_TOKENS,
                system_instruction=UI_INSTRUCTION,
            ),
        )
        super().__init__(name, llm=llm)
        self._snapshots = 0
        self._last_snapshot_at = 0.0

        # A turn that ends in plain text, with no tool called, is the answer:
        # spoken as the reply, so the request completes instead of hanging.
        @self.assistant_aggregator.event_handler("on_assistant_turn_stopped")
        async def _on_turn_stopped(aggregator, message):
            await self._on_plain_answer(aggregator, message)

    @tool
    async def select(self, params: FunctionCallParams, ref: Optional[str] = None, navigate: Optional[str] = None):
        """Click an element or switch screens and see the window afterwards, without answering yet.

        Use it when what you need appears only after the click, such as the
        memories of a Timeline block, or when the thing named is on another
        screen ("the third search" while Ask is showing: navigate to
        searches first); then call reply.

        Args:
            ref: Ref of the element to click, from the current state.
            navigate: Screen to switch to instead: ask, searches, timeline, watchers, or settings.
        """
        before = self._snapshots
        if navigate:
            view = navigate.strip().lower()
            if view in SCREENS:
                await self.send_command("navigate", Navigate(view=view))
            else:
                await params.result_callback(f"No screen named {navigate!r}.")
                return
        elif ref:
            await self.click(ref)
        else:
            await params.result_callback("Nothing to select: give a ref or a screen.")
            return
        # The page redraws and streams a new snapshot within a moment.
        for _ in range(30):
            await asyncio.sleep(0.1)
            if self._snapshots != before:
                break
        await asyncio.sleep(0.2)
        await params.result_callback(f"{'Switched to ' + navigate if navigate else 'Clicked ' + str(ref)}. The window now shows:\n{self.render_ui_state()}")

    @tool
    async def reply(
        self,
        params: FunctionCallParams,
        answer: Optional[str] = None,
        click: Optional[str] = None,
        navigate: Optional[str] = None,
        highlight: Optional[list[str]] = None,
        scroll_to: Optional[str] = None,
        timeline_day: Optional[str] = None,
        timeline_hour: Optional[int] = None,
        timeline_from: Optional[str] = None,
        timeline_to: Optional[str] = None,
    ):
        """Reply to the user and act on the window. Called exactly once per request.

        Args:
            answer: What to say, spoken aloud as is. One short sentence; may be left out for a silent action.
            click: Ref of an element to click, such as a memory card to open it or the Back button.
            navigate: Screen to switch to: ask, searches, timeline, watchers, or settings.
            highlight: Refs of elements to flash briefly, to point at them.
            scroll_to: Ref of an element to bring into view.
            timeline_day: Day to show on the Timeline, as YYYY-MM-DD.
            timeline_hour: Hour of that day to zoom into, 0 to 23.
            timeline_from: Start of a span to select on the Timeline, as HH:MM.
            timeline_to: End of that span, as HH:MM.
        """
        if timeline_day or timeline_hour is not None or timeline_from or timeline_to:
            await self.send_command(
                "timeline",
                {"day": timeline_day, "hour": timeline_hour, "from": timeline_from, "to": timeline_to},
            )
            navigate = None
        if navigate:
            view = navigate.strip().lower()
            if view in SCREENS:
                await self.send_command("navigate", Navigate(view=view))
            else:
                logger.warning(f"{self}: no screen named {navigate!r}")
        if scroll_to:
            await self.scroll_to(scroll_to)
        for ref in highlight or []:
            await self.highlight(ref)
        if click:
            await self.click(click)
        # No answer given: the action speaks for itself; the request completes quietly.
        await self.respond_to_job(answer or None, tts_speak=True)
        await params.result_callback(None)

    async def _on_plain_answer(self, aggregator, message):
        if self._pending is None or self._pending.done():
            return
        busy = getattr(aggregator, "has_function_calls_in_progress", False)
        if busy() if callable(busy) else busy:
            return
        text = getattr(message, "text", None)
        if text is None:
            content = getattr(message, "content", "")
            text = content if isinstance(content, str) else " ".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict)
            )
        text = (text or "").strip()
        if not text:
            return
        logger.debug(f"{self}: plain-text answer taken as the reply: {text[:80]!r}")
        await self.respond_to_job(text, tts_speak=True)

    async def _run_llm_turn(self, message) -> None:
        """The stock turn waits until a tool calls ``respond_to_job``; a turn
        that ends in plain text, or a model that goes quiet after ``select``,
        would leave the job open and every later request queued behind it.
        After a while the job is answered for it."""
        # The window may have just been opened for this request: give the
        # page a moment to send a fresh snapshot before the agent looks.
        if time.monotonic() - self._last_snapshot_at > FRESH_SNAPSHOT_SECS:
            before = self._snapshots
            for _ in range(int(SNAPSHOT_WAIT_SECS / 0.1)):
                await asyncio.sleep(0.1)
                if self._snapshots != before:
                    break
        turn = asyncio.ensure_future(super()._run_llm_turn(message))
        try:
            await asyncio.wait_for(asyncio.shield(turn), timeout=UI_TURN_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            # Plain text the model wrote was already spoken on its way; closing
            # the job silently avoids a second sentence on top of it.
            logger.warning(f"{self}: no reply within {UI_TURN_TIMEOUT_SECS:.0f}s; closing the request")
            await self.respond_to_job(None)
            await turn

    def render_query(self, message) -> str:
        # Relative days ("last Tuesday") need to know when now is.
        now = datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M")
        return f"(now: {now}) {super().render_query(message)}"

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, BusUIEventMessage) and message.event_name == _UI_SNAPSHOT_BUS_EVENT_NAME:
            self._snapshots += 1
            self._last_snapshot_at = time.monotonic()
            state = self.render_ui_state()
            logger.debug(f"{self}: snapshot, {state.count(chr(10)) + 1} lines, ~{len(state) // 4} tokens")
            logger.trace(f"{self}: {state}")
