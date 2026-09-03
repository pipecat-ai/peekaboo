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

SCREENS = ("ask", "searches", "timeline", "watchers", "settings")

UI_INSTRUCTION = """\
You operate the Peekaboo window for a user who is speaking, not typing.
Peekaboo remembers what was on their Mac's screen; the window shows those
memories. Its screens: Ask (a question box, the answer, and grids of memory
cards), Searches (past questions), Timeline (a day by the hour), Watchers,
Settings, and a Viewer that opens when a memory card is clicked (the full
screenshot, what was on screen, a filmstrip of neighbouring frames).

Each request comes with the current <ui_state>. Memory cards are buttons
named "<app> at <time>: <what was on screen>", listed in reading order in a
grid three across. Resolve "the first one", "the third screenshot", "the
terminal one", "this" against that state.

The Timeline works by voice exactly as by mouse. It has a month calendar
(days are buttons named "Tue 2 Sep: 8 memories"), the day's hours as tracks
(hour labels are buttons named "10:00: 12 memories"; clicking one zooms into
that hour, and zoomed in, the blocks on the track are buttons named
"Ghostty 10:05–10:12: 7 memories"), a "Selected …" line, and a strip of the
selected memories (buttons named "<app> at <time>: <what was on screen>",
the selected one tagged [selected]). Clicking a day, an hour, a block, or a
strip memory does what a mouse click does; clicking the strip memory that is
already selected opens it in the Viewer, so "the third one" selects it and
"open it" clicks it again. The timeline fields of [reply] do the rest:
`timeline_day` for a day ("last Tuesday", "yesterday", "the 14th": work out
the date from the current time given with the request), `timeline_hour` to
zoom into an hour, `timeline_from` and `timeline_to` to select a span of the
day. Any of them switches to the Timeline. "Show me 3 pm" is the hour;
"between ten and eleven" is a span; "this morning" is 06:00 to 12:00. Prefer
a click on a button that is in the state over the fields.

Questions about what the Timeline shows ("what was I working on in the last
block around three", "what is in that selection") are answered from the
state. If the block is not selected yet, call [select] with its ref first:
it clicks and returns what the window shows afterwards, with the block's
memories. Then answer with [reply] in two sentences from those memories'
names, naming the apps and the span. Do not send the user to a search.

[select] clicks without answering, for when you need to see the result
before you speak (a block's memories, another screen). Finish every request
with exactly one call to [reply]:
- To open a memory, pass its ref as `click`. To go back, click the "Back"
  button. To switch screens, pass `navigate` with one of: ask, searches,
  timeline, watchers, settings.
- To answer a question about what is shown ("what's the third one about"),
  read it off the state and say it; do not click unless asked to open it.
- `answer` is spoken aloud as is: one short sentence, plain words, no
  markup. For an action, a few words ("Opening the terminal one."). If the
  request does not match anything on the window, say so in one sentence.
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

    @tool
    async def select(self, params: FunctionCallParams, ref: str):
        """Click an element and see the window afterwards, without answering yet.

        Use it when what you need appears only after the click, such as the
        memories of a Timeline block; then call reply.

        Args:
            ref: Ref of the element to click, from the current state.
        """
        before = self._snapshots
        await self.click(ref)
        # The page redraws and streams a new snapshot within a moment.
        for _ in range(30):
            await asyncio.sleep(0.1)
            if self._snapshots != before:
                break
        await asyncio.sleep(0.2)
        await params.result_callback(f"Clicked {ref}. The window now shows:\n{self.render_ui_state()}")

    @tool
    async def reply(
        self,
        params: FunctionCallParams,
        answer: str,
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
            answer: What to say, spoken aloud as is. One short sentence.
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
        await self.respond_to_job(answer, tts_speak=True)
        await params.result_callback(None)

    def render_query(self, message) -> str:
        # Relative days ("last Tuesday") need to know when now is.
        now = datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M")
        return f"(now: {now}) {super().render_query(message)}"

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, BusUIEventMessage) and message.event_name == _UI_SNAPSHOT_BUS_EVENT_NAME:
            self._snapshots += 1
            state = self.render_ui_state()
            logger.debug(f"{self}: snapshot, {state.count(chr(10)) + 1} lines, ~{len(state) // 4} tokens")
            logger.trace(f"{self}: {state}")
