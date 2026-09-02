#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import os
import re
import webbrowser
from collections.abc import Callable
from typing import Literal, Optional

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.bus.messages import (
    BusJobResponseMessage,
    BusJobResponseUrgentMessage,
    BusJobUpdateMessage,
    BusJobUpdateUrgentMessage,
)
from pipecat.frames.frames import (
    FunctionCallResultProperties,
    LLMMessagesAppendFrame,
    LLMMessagesUpdateFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.job_context import JobParams, JobStatus
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport

from moments import Moment, MomentKind, MomentPolicy
from processors.conversation import ConversationState
from processors.screen_bridge import ScreenBridge
from workers.names import SCREEN_WORKER, VISION_WORKER, VOICE_WORKER

# A meeting reminder nobody acted on stops mattering a while after the start.
MEETING_MOMENT_LIFETIME_SECS = 20 * 60

# The voice LLM only routes: call a tool or not, and phrase short lines. The
# fastest tier is the right one; a slower model here is felt on every turn.
VOICE_MODEL = "claude-haiku-4-5"

# Spoken the moment a screen question goes out, instead of a second LLM call
# to phrase an acknowledgement. Saves about 1.3 s on every question.
LOOK_FILLER = "One moment."
WATCH_FILLER = "I'll let you know."

# Where speech recognition and synthesis run. "cloud" is Deepgram and
# Cartesia; "local" is Moonshine and Kokoro on the machine, imported only when
# chosen so the default start does not load ONNX models.
SpeechServices = Literal["cloud", "local"]

# Cartesia voice when CARTESIA_VOICE_ID is not set: British Reading Lady.
CARTESIA_VOICE = "71a7ad14-091c-4e8e-a314-022ece01c121"

# Kokoro voice: British English, female. Others: af_heart, bm_george, am_adam.
KOKORO_VOICE = "bf_emma"

# Kokoro on the CPU synthesizes a long paragraph in one go and hands it over
# whole, so a TTS context can sit silent for a few seconds before its audio
# arrives; the default 3 s idle timeout closes it as silent. Answers are spoken
# sentence by sentence to keep each context short; this is the backstop.
TTS_IDLE_TIMEOUT_SECS = 15.0

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")

SYSTEM_INSTRUCTION = """

You are a voice assistant. You cannot see the screen yourself and you have no
memory of it. The [get_vision_help] tool has both: the screen right now, and a
searchable record of everything that was on screen going back days. Its
answers are delivered to the user separately, after your turn.

Tool-use rules:

- If the user asks a general knowledge question, DO NOT call any tool.

- If the user asks about anything that is or was on the screen, at any time,
  today or days ago, call [get_vision_help]. Never say you have no access to
  the past; the tool does. NEVER answer the question yourself.

- If the user wants to be told when something happens on screen, call
  [get_vision_help] with watchlist set to true.

- When you call [get_vision_help], call it without saying anything first. An
  acknowledgement is spoken for you, and the answer arrives separately.

- Reminders about meetings or scheduled events that appeared on the screen
  arrive as developer messages. Tell the user in one short sentence and, if
  there is a link, ask whether to open it. When they agree, call
  [join_meeting]. If they ask for later, call [snooze_reminder]. Never read a
  URL aloud.

- If unsure, ALWAYS double-check with the user before calling any tool.

Be extremely brief. All responses are spoken aloud. Avoid emojis, bullet points,
or anything difficult to vocalize.

"""


class VoiceWorker(PipelineWorker):
    """The conversation: transport, STT, LLM, TTS.

    Screen questions become ``look`` jobs on the vision worker and watch
    requests become ``watch`` jobs on the screen worker. Whatever comes back,
    an answer, a progress line, a watch hit, or a reminder the screen worker
    spotted on screen, becomes a moment, and the moment policy decides when
    it is spoken: never over anyone talking, and unsolicited ones not while a
    quiet rule holds.

    When the screen is shared through the transport (a browser session), a
    screen bridge right after the transport input carries its frames to the
    vision worker over the bus. On a Mac the vision worker reads the screen
    from the OS and this pipeline carries audio only.
    """

    def __init__(
        self,
        transport: BaseTransport,
        *,
        vision_worker: str = VISION_WORKER,
        screen_worker: str = SCREEN_WORKER,
        screen_from_transport: bool = True,
        open_links: bool = True,
        speech: SpeechServices = "cloud",
        quiet_checks: Optional[list[Callable[[], bool]]] = None,
        idle_timeout_secs: float | None = None,
        **kwargs,
    ):
        self._transport = transport
        self._vision_worker = vision_worker
        self._screen_worker = screen_worker
        self._open_links = open_links
        self._speech = speech
        self._screen_bridge = (
            ScreenBridge(screen_worker_name=screen_worker) if screen_from_transport else None
        )
        self._state = ConversationState()
        self._moments = MomentPolicy(
            is_idle=lambda: self._state.idle,
            speak=self._speak_moment,
            banner=self._banner_moment,
            quiet_checks=quiet_checks,
        )
        self._moments_task: Optional[asyncio.Task] = None

        # Which jobs are answers to the user and which are watches, so what
        # comes back on them can be queued as the right kind of moment.
        self._look_jobs: set[str] = set()
        self._watch_jobs: set[str] = set()

        pipeline = self._build_pipeline()

        super().__init__(
            pipeline,
            name=VOICE_WORKER,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            idle_timeout_secs=idle_timeout_secs,
            **kwargs,
        )

    def _speech_services(self):
        if self._speech == "local":
            # Speech in both directions stays on the machine: Moonshine
            # transcribes each turn once it ends (ONNX on the CPU), Kokoro
            # synthesizes. Both download their models on first use.
            from pipecat.services.kokoro.tts import KokoroTTSService
            from pipecat.services.moonshine.stt import MoonshineSTTService

            stt = MoonshineSTTService()
            tts = KokoroTTSService(
                settings=KokoroTTSService.Settings(voice=KOKORO_VOICE),
                stop_frame_timeout_s=TTS_IDLE_TIMEOUT_SECS,
            )
        else:
            stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
            tts = CartesiaTTSService(
                api_key=os.getenv("CARTESIA_API_KEY"),
                settings=CartesiaTTSService.Settings(
                    voice=os.getenv("CARTESIA_VOICE_ID") or CARTESIA_VOICE
                ),
            )
        return stt, tts

    def _build_pipeline(self) -> Pipeline:
        stt, tts = self._speech_services()

        llm = AnthropicLLMService(
            name="VoiceAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            # A request that hangs on connect is retried once.
            retry_on_timeout=True,
            settings=AnthropicLLMService.Settings(
                model=VOICE_MODEL, system_instruction=SYSTEM_INSTRUCTION
            ),
        )
        llm.register_function("get_vision_help", self._get_vision_help)
        llm.register_function("join_meeting", self._join_meeting)
        llm.register_function("snooze_reminder", self._snooze_reminder)

        vision_function = FunctionSchema(
            name="get_vision_help",
            description=(
                "Call this function whenever the user asks about something on their screen: "
                "what is visible now, what was on screen earlier today or on a past day, or "
                "something they expect to appear later. This includes questions about UI "
                "elements, text, images, buttons, errors, and anything they looked at before."
            ),
            properties={
                "query": {
                    "type": "string",
                    "description": "The exact question the user is asking.",
                },
                "watchlist": {
                    "type": "boolean",
                    "description": (
                        "Set to true if the user wants to be notified repeatedly whenever "
                        "a relevant visual event occurs (e.g., when a window appears, "
                        "a button becomes enabled, a value changes, etc.)."
                    ),
                },
            },
            required=["query", "watchlist"],
        )

        join_function = FunctionSchema(
            name="join_meeting",
            description=(
                "Open the join link of the meeting that was just announced. Call it when the "
                "user agrees to join or open the meeting."
            ),
            properties={},
            required=[],
        )

        snooze_function = FunctionSchema(
            name="snooze_reminder",
            description="Remind the user about the announced meeting again in a few minutes.",
            properties={
                "minutes": {
                    "type": "integer",
                    "description": "How many minutes from now to remind again. Defaults to 5.",
                },
            },
            required=[],
        )

        context = LLMContext(
            tools=ToolsSchema(standard_tools=[vision_function, join_function, snooze_function])
        )

        # Turn detection lives here: VAD decides when the user starts talking
        # and the default stop strategy is the local Smart Turn v3 analyzer.
        aggregators = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
            ),
        )

        processors = [
            self._transport.input(),
            stt,
            aggregators.user(),
            llm,
            self._state,  # Who is talking, for the moment policy
            tts,
            self._transport.output(),
            aggregators.assistant(),
        ]
        if self._screen_bridge:
            # Right after the input: screen frames out to the vision worker,
            # its frame requests back in.
            processors.insert(1, self._screen_bridge)

        return Pipeline(processors)

    async def start_session(self, client_id: str):
        """Kick off the conversation once the client is connected.

        Call after screen capture is enabled on the transport.
        """
        if self._screen_bridge:
            self._screen_bridge.set_client_id(client_id)

        # A fresh conversation per connection.
        await self.queue_frame(
            LLMMessagesUpdateFrame(
                messages=[{"role": "developer", "content": "Ask the user how you can help."}],
                run_llm=True,
            )
        )

        # Screen capture is on: let the screen worker start asking for frames.
        await self.request_job(
            self._screen_worker, params=JobParams(name="capture", payload={"action": "start"})
        )

        # Reminders the screen worker spots on screen, delivered as updates on
        # this long-lived job.
        await self.request_job(self._screen_worker, params=JobParams(name="subscribe"))

        if self._moments_task is None:
            self._moments_task = self.create_task(self._moments.run(), name="moments")

    async def cleanup(self):
        # The moment policy runs for the life of the session; take it down
        # with the worker so shutdown leaves nothing dangling.
        if self._moments_task:
            task, self._moments_task = self._moments_task, None
            await self.cancel_task(task)
        await super().cleanup()

    async def say(self, text: str):
        """Speak text directly, bypassing the LLM.

        One TTS request per sentence: the first sentence is playing while the
        rest are still being synthesized, and no single request runs long
        enough to be closed as silent.
        """
        logger.info(f"{self}: saying: {text}")
        for sentence in _SENTENCE_END.split(text.strip()):
            if sentence:
                await self.queue_frame(TTSSpeakFrame(text=sentence))

    #
    # Moments
    #

    async def _speak_moment(self, moment: Moment):
        if moment.text:
            await self.say(moment.text)
        if moment.prompt:
            logger.info(f"{self}: prompting: {moment.prompt[:80]}")
            await self.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "developer", "content": moment.prompt}],
                    run_llm=True,
                )
            )

    async def _banner_moment(self, moment: Moment):
        # A UI worker will turn this into a system banner. Until then it is a
        # log line, and the moment is spoken once the quiet rule lifts.
        logger.info(f"{self}: banner: {moment.text or moment.prompt}")

    def _meeting_moment(self, reminder: dict) -> Moment:
        text = reminder.get("text") or "a meeting is starting"
        link = reminder.get("join_url")
        prompt = (
            f"A notification on the screen says: \"{text}\". "
            + (
                "A join link is visible; ask whether to open it and call join_meeting if so. "
                if link
                else "No join link is visible; if it is a meeting, ask whether they want to join. "
            )
            + "Tell the user in one short sentence. Do not read any URL aloud."
        )
        return Moment(
            kind=MomentKind.MEETING,
            prompt=prompt,
            url=link,
            expires_at=asyncio.get_running_loop().time() + MEETING_MOMENT_LIFETIME_SECS,
        )

    async def _get_vision_help(self, params: FunctionCallParams):
        query = params.arguments["query"]
        watchlist = params.arguments["watchlist"]

        # Fire and forget: the answer, or the watch hits, come back as job
        # messages and are spoken then. A canned acknowledgement goes out now
        # and the LLM is not run again for it; the result only records in the
        # context what was said.
        if watchlist:
            job_id = await self.request_job(
                self._screen_worker, params=JobParams(name="watch", payload={"query": query})
            )
            self._watch_jobs.add(job_id)
            filler = WATCH_FILLER
        else:
            job_id = await self.request_job(
                self._vision_worker, params=JobParams(name="look", payload={"query": query})
            )
            self._look_jobs.add(job_id)
            filler = LOOK_FILLER

        await self.say(filler)
        await params.result_callback(
            f"Acknowledged to the user with: \"{filler}\" The answer will be spoken separately.",
            properties=FunctionCallResultProperties(run_llm=False),
        )

    async def _join_meeting(self, params: FunctionCallParams):
        meeting = self._moments.last_meeting
        if not meeting or not meeting.url:
            await params.result_callback({"opened": False, "reason": "no meeting link"})
            return
        if self._open_links:
            await asyncio.to_thread(webbrowser.open, meeting.url)
            logger.info(f"{self}: opened {meeting.url}")
        else:
            logger.info(f"{self}: would open {meeting.url}")
        await params.result_callback({"opened": True})

    async def _snooze_reminder(self, params: FunctionCallParams):
        meeting = self._moments.last_meeting
        if not meeting:
            await params.result_callback({"snoozed": False, "reason": "no reminder to snooze"})
            return
        minutes = int(params.arguments.get("minutes") or 5)
        self._moments.snooze(meeting, minutes)
        await params.result_callback({"snoozed": True, "minutes": minutes})

    #
    # Job results
    #

    def _kind_for(self, job_id: str) -> MomentKind:
        return MomentKind.WATCH if job_id in self._watch_jobs else MomentKind.ANSWER

    async def on_job_update(self, message: BusJobUpdateMessage | BusJobUpdateUrgentMessage):
        await super().on_job_update(message)
        update = message.update or {}
        if update.get("moment"):
            self._moments.enqueue(self._meeting_moment(update["moment"]))
            return
        text = update.get("say")
        if text:
            self._moments.enqueue(Moment(kind=self._kind_for(message.job_id), text=text))

    async def on_job_response(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_response(message)
        self._look_jobs.discard(message.job_id)
        self._watch_jobs.discard(message.job_id)
        if message.status == JobStatus.CANCELLED:
            return
        text = (message.response or {}).get("answer")
        if text:
            self._moments.enqueue(Moment(kind=MomentKind.ANSWER, text=text))

    async def on_job_error(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_error(message)
        self._look_jobs.discard(message.job_id)
        self._watch_jobs.discard(message.job_id)
        logger.warning(f"{self}: job {message.job_id} failed: {message.response}")
        text = (message.response or {}).get("answer") or "Sorry, I couldn't check that."
        self._moments.enqueue(Moment(kind=MomentKind.ANSWER, text=text))
