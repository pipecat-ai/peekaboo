#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
from datetime import datetime
import io
import os
import re
import wave
import webbrowser
from pathlib import Path
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, Literal, Optional

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
    Frame,
    FunctionCallResultProperties,
    LLMMessagesAppendFrame,
    LLMMessagesUpdateFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.bus.messages import BusJobRequestMessage
from pipecat.pipeline.job_context import JobError, JobParams, JobStatus
from pipecat.pipeline.job_decorator import job
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaHttpTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.moonshine.stt import MoonshineSTTService
from pipecat.transports.base_transport import BaseTransport

from moments import Moment, MomentKind, MomentPolicy
from processors.conversation import ConversationState
from processors.wake import LocalTranscriptionFrame, WakeGate
from processors.screen_bridge import ScreenBridge
from workers.names import SCREEN_WORKER, UI_WORKER, VISION_WORKER, VOICE_WORKER

if TYPE_CHECKING:
    from store.sqlite_store import SQLiteStore
    from macos.registry import WindowRegistry

# A meeting reminder nobody acted on stops mattering a while after the start.
MEETING_MOMENT_LIFETIME_SECS = 20 * 60

# The voice LLM only routes: call a tool or not, and phrase short lines. The
# fastest tier is the right one; a slower model here is felt on every turn.
VOICE_MODEL = "claude-haiku-4-5"

# Spoken the moment a screen question goes out, instead of a second LLM call
# to phrase an acknowledgement. Saves about 1.3 s on every question.
LOOK_FILLER = "One moment."
WATCH_FILLER = "I'll let you know."

# Spoken on launch. The first time a voice says it the audio is kept as a WAV
# under the store, and every launch after that plays the file: no TTS call.
GREETING = "Welcome to Peekaboo."

# list_windows is read to a model, not a person; keep it short.
MAX_LISTED_WINDOWS = 25

# Where speech runs. Recognition always starts on the machine: Moonshine hears
# everything and only what follows the wake phrase goes further. "cloud" then
# connects Deepgram for the conversation and speaks through Cartesia over
# HTTP, a request per utterance, so nothing is connected while idle. "local"
# stays on the machine throughout, with Kokoro speaking.
SpeechServices = Literal["cloud", "local"]

# Spoken when the wake phrase comes alone.
WAKE_ACK = "Yes?"

# Silence that ends an utterance. Long enough that "Peekaboo," and the
# question after it are heard as one, short enough not to drag the turn end.
WAKE_VAD_STOP_SECS = 0.6

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

You are a voice assistant on the user's Mac. You cannot see the screen yourself
and you have no memory of it; your tools do. [look] sees the screen or one
window right now and also has a searchable record of everything that was on
screen going back days. [watch] tells the user when something happens on the
screen or in one window. Answers and watch hits are delivered to the user
separately, after your turn.

Targets: when the user names a window or an app ("the terminal", "Chrome",
"the build window"), pass what they said as the target. Leave the target empty
for the whole screen. [list_windows] tells you what is open.

Tool-use rules:

- If the user asks a general knowledge question, DO NOT call any tool.

- Say times the way a person would: "ten fifteen in the morning", "a quarter
  past three in the afternoon", "around five PM"; never seconds, never
  24-hour or ISO forms, and no date when it is today.

- If the user asks about anything that is or was on the screen, at any time,
  today or days ago, call [look]. Never say you have no access to the past;
  the tool does. NEVER answer the question yourself.

- If the user wants to be told when something happens, call [watch] with the
  condition in their words. To stop, call [unwatch]. [list_watchers] says
  what is being watched.

- When you call [look] or [watch], call it without saying anything first. An
  acknowledgement is spoken for you, and the result arrives separately.

- After an answer about the past, "show me" means call [show_me]: it opens
  the screenshots behind that answer in a window.

- If the user refers to something they asked or searched before ("what did I
  ask yesterday", "I remember searching for", "what did you tell me about"),
  call [past_searches] and answer from what it returns: when they asked, what
  they asked, and what the answer was, briefly.

- If the user talks about the Peekaboo window or what it shows ("open the
  first one", "go to the timeline", "what's the third screenshot about",
  "go back", "show the searches"), call [window] with their words and say
  nothing yourself: the window answers and acts on its own. "Show me the
  watchers", "show me the timeline", "show me the searches", "show me the
  settings", "open Peekaboo" are window navigation, not [show_me] and not
  [list_watchers]. "This image", "this screenshot", "this one", "what is
  this about", "what was this" refer to the memory open in the window: call
  [window], never [look]. Never call [look] with Peekaboo as the target;
  Peekaboo's own window is never the subject of a look. Anything about
  clicking, opening, selecting, taking a look at, or seeing details of an
  image, screenshot, block or memory is for [window], including follow-ups
  such as "can we take a look", "click on that", "open it", "more details":
  never answer those yourself, never say you cannot click. That includes
  questions about what the Timeline shows: a block, an hour, a selection,
  "the last block around three", "what was I doing in that one". Those are
  about what is on the window, not a search of the past; [look] is for the
  past when nothing on the window is being pointed at.

- "Start recording", "pause recording", "stop recording" mean call
  [set_recording]. Recording is what builds the memory of the screen.

- Reminders about meetings or scheduled events that appeared on the screen
  arrive as developer messages. Tell the user in one short sentence and, if
  there is a link, ask whether to open it. When they agree, call
  [join_meeting]. If they ask for later, call [snooze_reminder]. Never read a
  URL aloud.

- If unsure, ALWAYS double-check with the user before calling any tool.

Be extremely brief. All responses are spoken aloud. Avoid emojis, bullet points,
or anything difficult to vocalize.

"""


# Cached audio is played in chunks this long, like TTS output.
PLAYBACK_CHUNK_SECS = 0.02


class LocalMoonshineSTTService(MoonshineSTTService):
    """Moonshine whose transcripts are marked as local, so the wake gate can
    tell them from the cloud recognizer's."""

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        async for frame in super().run_stt(audio):
            if isinstance(frame, TranscriptionFrame):
                frame = LocalTranscriptionFrame(
                    text=frame.text,
                    user_id=frame.user_id,
                    timestamp=frame.timestamp,
                    language=frame.language,
                    result=frame.result,
                    finalized=frame.finalized,
                )
            yield frame


# Audio kept while the cloud recognizer connects: the question that follows a
# bare "Peekaboo" must not be lost to the handshake.
WAKE_BUFFER_MAX_BYTES = 16000 * 2 * 10


class OnDemandDeepgramSTTService(DeepgramSTTService):
    """Deepgram that is connected only while the wake gate is awake.

    The stock service connects in ``setup`` and holds the websocket for the
    life of the pipeline. This one waits for :meth:`wake`, and :meth:`sleep`
    closes the connection again. Asleep, audio is dropped; while the
    connection is being made it is kept and sent first once it is up.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._waking = False
        self._pending = bytearray()

    async def setup(self, setup: FrameProcessorSetup):
        # Everything the parent sets up, minus the connection.
        await super(DeepgramSTTService, self).setup(setup)

    async def wake(self):
        if self._connection_task or self._waking:
            return
        logger.info(f"{self}: connecting")
        self._waking = True
        self._pending = bytearray()
        self.create_task(self._connect_in_background(), name="deepgram-wake")

    async def _connect_in_background(self):
        try:
            await self._connect()
        finally:
            self._waking = False

    async def sleep(self):
        self._pending = bytearray()
        if self._connection_task:
            logger.info(f"{self}: disconnecting")
            await self._disconnect()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if not self._connection:
            if self._waking:
                self._pending += audio
                if len(self._pending) > WAKE_BUFFER_MAX_BYTES:
                    del self._pending[: len(self._pending) - WAKE_BUFFER_MAX_BYTES]
            yield None
            return
        if self._pending:
            held, self._pending = bytes(self._pending), bytearray()
            async for frame in super().run_stt(held):
                yield frame
        async for frame in super().run_stt(audio):
            yield frame


class GreetingRecorder(FrameProcessor):
    """Sits after TTS. When armed, keeps the next utterance's audio and writes
    it to a WAV once it ends, then stands down. Everything passes through.

    It also plays cached audio: pushed from here it reaches only the transport
    output. Queued at the head of the pipeline it would pass through STT and
    be transcribed as the user's words, and the LLM would answer it.
    """

    async def play(self, pcm: bytes, rate: int):
        """Play PCM as if TTS had produced it."""
        await self.push_frame(TTSStartedFrame())
        step = int(rate * PLAYBACK_CHUNK_SECS) * 2
        for start in range(0, len(pcm), step):
            await self.push_frame(TTSAudioRawFrame(audio=pcm[start : start + step], sample_rate=rate, num_channels=1))
        await self.push_frame(TTSStoppedFrame())

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._path: Optional[Path] = None
        self._chunks: list[bytes] = []
        self._rate = 0

    def arm(self, path: Path):
        self._path = path
        self._chunks = []
        self._rate = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self._path is not None:
            if isinstance(frame, TTSAudioRawFrame) and frame.num_channels == 1:
                self._chunks.append(frame.audio)
                self._rate = frame.sample_rate
            elif isinstance(frame, TTSStoppedFrame):
                self._finish()
        await self.push_frame(frame, direction)

    def _finish(self):
        path, self._path = self._path, None
        if not self._chunks or not self._rate:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self._rate)
                w.writeframes(b"".join(self._chunks))
            logger.info(f"{self}: greeting cached to {path}")
        except OSError as e:
            logger.warning(f"{self}: could not cache the greeting: {e}")


class VoiceWorker(PipelineWorker):
    """The conversation: transport, STT, LLM, TTS.

    Screen questions become ``look`` jobs on the vision worker and watch
    requests become ``watch`` jobs on the screen worker, each carrying the
    window or app the user named. Whatever comes back, an answer, a progress
    line, a watch hit, a warning that a watched window went out of sight, or
    a reminder the screen worker spotted on screen, becomes a moment, and the
    moment policy decides when it is spoken: never over anyone talking, and
    unsolicited ones not while a quiet rule holds.

    ``list_windows`` answers from the window registry in-process; without one
    (a browser session) only the shared screen exists.

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
        ui_worker: str = UI_WORKER,
        screen_from_transport: bool = True,
        open_links: bool = True,
        speech: SpeechServices = "cloud",
        registry: Optional["WindowRegistry"] = None,
        on_state: Optional[Callable[[str], None]] = None,
        on_show: Optional[Callable[[list[int]], None]] = None,
        on_asked: Optional[Callable[[str], None]] = None,
        on_show_ask: Optional[Callable[[int], None]] = None,
        on_show_screen: Optional[Callable[[str], None]] = None,
        on_open_window: Optional[Callable[[], None]] = None,
        store: Optional["SQLiteStore"] = None,
        on_answer: Optional[Callable[[str, str, list[int]], Awaitable[None]]] = None,
        on_recording: Optional[Callable[[bool], Awaitable[None]]] = None,
        greeting_cache: Optional[Path] = None,
        wake_word: bool = True,
        quiet_checks: Optional[list[Callable[[], bool]]] = None,
        idle_timeout_secs: float | None = None,
        **kwargs,
    ):
        self._transport = transport
        self._vision_worker = vision_worker
        self._screen_worker = screen_worker
        self._ui_worker = ui_worker
        self._open_links = open_links
        self._speech = speech
        self._registry = registry
        self._on_show = on_show
        self._on_asked = on_asked
        self._on_show_ask = on_show_ask
        self._on_show_screen = on_show_screen
        self._on_open_window = on_open_window
        self._store = store
        self._on_answer = on_answer
        self._on_recording = on_recording
        # The question behind each look job, for the memories window.
        self._look_questions: dict[str, str] = {}
        self._recording = True
        self._greeting_cache = greeting_cache
        self._greeting_recorder = GreetingRecorder()
        self._wake_word = wake_word
        self._wake_gate: Optional[WakeGate] = None
        self._cloud_stt: Optional[OnDemandDeepgramSTTService] = None
        # The observations behind the last spoken answer, for "show me".
        self._last_ids: list[int] = []
        self._screen_bridge = (
            ScreenBridge(screen_worker_name=screen_worker) if screen_from_transport else None
        )
        self._state = ConversationState(on_change=on_state)
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

    def _speech_services(self) -> list[FrameProcessor]:
        """The recognition stage and the synthesizer: ``[*stt, tts]``.

        Recognition is Moonshine on the machine, always, hearing everything
        (its transcripts are what the wake gate reads). In cloud mode Deepgram
        follows it, connected only while the gate is awake, and Cartesia
        speaks over HTTP. In local mode Kokoro speaks and nothing is ever
        connected.
        """
        # Audio passes through Moonshine so the cloud recognizer behind it
        # can hear too, once awake. The gate comes last and sees both
        # recognizers' transcripts.
        stage: list[FrameProcessor] = [LocalMoonshineSTTService(audio_passthrough=True)]
        if self._speech == "local":
            from pipecat.services.kokoro.tts import KokoroTTSService

            tts = KokoroTTSService(
                settings=KokoroTTSService.Settings(voice=KOKORO_VOICE),
                stop_frame_timeout_s=TTS_IDLE_TIMEOUT_SECS,
            )
        else:
            self._cloud_stt = OnDemandDeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
            stage.append(self._cloud_stt)
            tts = CartesiaHttpTTSService(
                api_key=os.getenv("CARTESIA_API_KEY"),
                settings=CartesiaHttpTTSService.Settings(voice=os.getenv("CARTESIA_VOICE_ID") or CARTESIA_VOICE),
            )
        if self._wake_word:
            self._wake_gate = WakeGate(
                on_wake=self._on_wake,
                on_sleep=self._on_sleep,
                on_acknowledge=lambda: self.say(WAKE_ACK),
            )
            stage.append(self._wake_gate)
        return [*stage, tts]

    async def set_listening(self, on: bool):
        """Mute or unmute the microphone. Muting also puts the wake gate to
        sleep, so the cloud recognizer disconnects and nothing is heard."""
        self._transport.set_muted(not on)
        if not on and self._wake_gate is not None:
            await self._wake_gate.sleep()
        logger.info(f"{self}: {'listening' if on else 'not listening'}")

    async def _on_wake(self):
        if self._cloud_stt:
            await self._cloud_stt.wake()

    async def _on_sleep(self):
        if self._cloud_stt:
            await self._cloud_stt.sleep()

    def _voice_key(self) -> str:
        """Which voice speaks: the greeting cache is per voice."""
        if self._speech == "local":
            return f"kokoro-{KOKORO_VOICE}"
        return f"cartesia-{os.getenv('CARTESIA_VOICE_ID') or CARTESIA_VOICE}"

    def _build_pipeline(self) -> Pipeline:
        *stt, tts = self._speech_services()

        llm = AnthropicLLMService(
            name="VoiceAnthropicLLMService",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            # A request that hangs on connect is retried once.
            retry_on_timeout=True,
            settings=AnthropicLLMService.Settings(
                model=VOICE_MODEL, system_instruction=SYSTEM_INSTRUCTION
            ),
        )
        llm.register_function("look", self._look)
        llm.register_function("watch", self._watch)
        llm.register_function("unwatch", self._unwatch)
        llm.register_function("list_watchers", self._list_watchers)
        llm.register_function("list_windows", self._list_windows)
        llm.register_function("show_me", self._show_me)
        llm.register_function("past_searches", self._past_searches)
        llm.register_function("window", self._window)
        llm.register_function("set_recording", self._set_recording)
        llm.register_function("join_meeting", self._join_meeting)
        llm.register_function("snooze_reminder", self._snooze_reminder)

        target_property = {
            "type": "string",
            "description": (
                "The window or app the user named, in their words: 'the terminal', 'Chrome', "
                "part of a window title. Empty for the whole screen."
            ),
        }

        look_function = FunctionSchema(
            name="look",
            description=(
                "Answer a question about the screen or one window: what is visible now, or what "
                "was on screen earlier today or on a past day. UI, text, errors, numbers, links, "
                "anything the user looked at."
            ),
            properties={
                "question": {"type": "string", "description": "The exact question the user is asking."},
                "target": target_property,
            },
            required=["question"],
        )

        watch_function = FunctionSchema(
            name="watch",
            description=(
                "Tell the user when something happens on the screen or in one window: a build "
                "finishing, a message arriving, a value changing, a window appearing."
            ),
            properties={
                "condition": {"type": "string", "description": "What to watch for, in the user's words."},
                "target": target_property,
            },
            required=["condition"],
        )

        unwatch_function = FunctionSchema(
            name="unwatch",
            description=(
                "Stop watching. Give the watcher id from list_watchers, or the target to stop "
                "every watcher on it, or nothing to stop them all."
            ),
            properties={
                "id": {"type": "integer", "description": "A watcher id from list_watchers."},
                "target": target_property,
            },
            required=[],
        )

        list_watchers_function = FunctionSchema(
            name="list_watchers",
            description=(
                "What is currently being watched, with ids, to say it aloud when the user asks "
                "what is being watched. Not for showing the Watchers screen: that is [window]."
            ),
            properties={},
            required=[],
        )

        recording_function = FunctionSchema(
            name="set_recording",
            description="Start or pause recording the screen into memory.",
            properties={"on": {"type": "boolean", "description": "True to record, false to pause."}},
            required=["on"],
        )

        show_me_function = FunctionSchema(
            name="show_me",
            description=(
                "Open the memories window on the results behind the last answer: the answer and "
                "the memories it drew on. Call it when the user says 'show me' or asks to see it; "
                "opening one of them is then a request for [window]."
            ),
            properties={},
            required=[],
        )

        past_searches_function = FunctionSchema(
            name="past_searches",
            description=(
                "Find questions the user asked before and what was answered. Call it when the "
                "user refers to an earlier question or search: 'what did I ask yesterday', "
                "'I remember searching for...', 'what did you tell me about...'."
            ),
            properties={
                "query": {
                    "type": "string",
                    "description": "A few words from the earlier question or its answer. Empty for the most recent ones.",
                },
            },
            required=["query"],
        )
        window_function = FunctionSchema(
            name="window",
            description=(
                "Operate the Peekaboo window, or answer about what it shows. Call it when the "
                "user refers to the window or something on it: open/show/click/select/go back, "
                "'the first one', 'the third screenshot', 'this one', 'take a look', 'more "
                "details', switch to the timeline, searches, watchers or settings, or asks what "
                "one of the shown memories is about. The window can click anything it shows."
            ),
            properties={
                "request": {
                    "type": "string",
                    "description": "What the user wants, in their words.",
                },
            },
            required=["request"],
        )
        list_windows_function = FunctionSchema(
            name="list_windows",
            description="The apps and windows open right now, front to back.",
            properties={
                "app": {"type": "string", "description": "Only this app's windows. Empty for all."},
            },
            required=[],
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
            tools=ToolsSchema(
                standard_tools=[
                    look_function,
                    watch_function,
                    unwatch_function,
                    list_watchers_function,
                    list_windows_function,
                    show_me_function,
                    past_searches_function,
                    window_function,
                    recording_function,
                    join_function,
                    snooze_function,
                ]
            )
        )

        # Turn detection lives here: VAD decides when the user starts talking
        # and the default stop strategy is the local Smart Turn v3 analyzer.
        # The VAD's stop also ends the local recognizer's segments; the pause
        # after "Peekaboo," must not cut the word off into its own clip, where
        # small models mangle it, so the stop is longer than the default.
        aggregators = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=WAKE_VAD_STOP_SECS)),
            ),
        )

        processors = [
            self._transport.input(),
            *stt,
            aggregators.user(),
            llm,
            self._state,  # Who is talking, for the moment policy
            tts,
            self._greeting_recorder,  # Keeps the greeting's audio the first time
            self._transport.output(),
            aggregators.assistant(),
        ]
        if self._screen_bridge:
            # Right after the input: screen frames out to the vision worker,
            # its frame requests back in.
            processors.insert(1, self._screen_bridge)

        return Pipeline(processors)

    async def start_session(self, client_id: str, *, recording: bool = True):
        """Kick off the conversation once the client is connected.

        Call after screen capture is enabled on the transport. With
        ``recording`` false the screen worker is left idle until asked.
        """
        if self._screen_bridge:
            self._screen_bridge.set_client_id(client_id)
        self._recording = recording

        # A fresh conversation per connection, opened with a fixed line and no
        # LLM call; from the cache when this voice has said it before.
        await self.queue_frame(LLMMessagesUpdateFrame(messages=[], run_llm=False))
        await self._greet()

        if recording:
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

    async def _greet(self):
        cached = self._greeting_cache / f"{self._voice_key()}.wav" if self._greeting_cache else None
        if cached and cached.exists():
            try:
                with wave.open(str(cached), "rb") as w:
                    rate, pcm = w.getframerate(), w.readframes(w.getnframes())
                logger.info(f"{self}: greeting from {cached.name}")
                await self._greeting_recorder.play(pcm, rate)
                return
            except (OSError, wave.Error) as e:
                logger.warning(f"{self}: cached greeting unusable, speaking it: {e}")
        if cached:
            self._greeting_recorder.arm(cached)
        await self.say(GREETING)

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

    async def _look(self, params: FunctionCallParams):
        question = str(params.arguments.get("question", ""))
        target = str(params.arguments.get("target") or "")

        # Fire and forget: the answer comes back as a job message and is
        # spoken then. A canned acknowledgement goes out now and the LLM is
        # not run again for it; the result only records what was said.
        job_id = await self.request_job(
            self._vision_worker,
            params=JobParams(name="look", payload={"query": question, "target": target}),
        )
        self._look_jobs.add(job_id)
        self._look_questions[job_id] = question
        if self._on_asked:
            self._on_asked(question)
        await self._acknowledge(params, LOOK_FILLER)

    async def _watch(self, params: FunctionCallParams):
        condition = str(params.arguments.get("condition", ""))
        target = str(params.arguments.get("target") or "")
        await self._start_watch(condition, target)
        await self._acknowledge(params, WATCH_FILLER)

    async def _start_watch(self, condition: str, target: str) -> str:
        job_id = await self.request_job(
            self._screen_worker,
            params=JobParams(name="watch", payload={"query": condition, "target": target}),
        )
        self._watch_jobs.add(job_id)
        return job_id

    @job(name="watch")
    async def _watch_job(self, message: BusJobRequestMessage):
        """A watcher asked for from the app rather than aloud. Created here so
        its hits become spoken moments like any other."""
        payload = message.payload or {}
        job_id = await self._start_watch(str(payload.get("condition", "")), str(payload.get("target") or ""))
        await self.send_job_response(message.job_id, {"started": True, "job_id": job_id})

    async def _acknowledge(self, params: FunctionCallParams, filler: str):
        await self.say(filler)
        await params.result_callback(
            f"Acknowledged to the user with: \"{filler}\" The result will be spoken separately.",
            properties=FunctionCallResultProperties(run_llm=False),
        )

    async def _unwatch(self, params: FunctionCallParams):
        payload = {}
        if params.arguments.get("id") is not None:
            payload["id"] = int(params.arguments["id"])
        if params.arguments.get("target"):
            payload["target"] = str(params.arguments["target"])
        try:
            async with self.job(self._screen_worker, params=JobParams(name="unwatch", payload=payload)) as t:
                pass
        except JobError as e:
            await params.result_callback({"error": str(e)})
            return
        await params.result_callback(t.response or {})

    async def _list_watchers(self, params: FunctionCallParams):
        # Asked about the watchers, the window shows them too.
        if self._on_show_screen:
            self._on_show_screen("watchers")
        try:
            async with self.job(self._screen_worker, params=JobParams(name="list_watchers")) as t:
                pass
        except JobError as e:
            await params.result_callback({"error": str(e)})
            return
        await params.result_callback(t.response or {})

    async def _set_recording(self, params: FunctionCallParams):
        on = bool(params.arguments.get("on", True))
        self._recording = on
        if self._on_recording is not None:
            await self._on_recording(on)
        else:
            await self.request_job(
                self._screen_worker,
                params=JobParams(name="capture", payload={"action": "start" if on else "stop"}),
            )
        await params.result_callback({"recording": on})

    async def _window(self, params: FunctionCallParams):
        # The ui worker sees the window's accessibility snapshot, acts on the
        # page, and speaks its own short reply through this pipeline's TTS.
        # Asking the window for something means wanting to see it.
        if self._on_open_window:
            self._on_open_window()
        request = str(params.arguments.get("request") or "")
        await self.request_job(self._ui_worker, params=JobParams(name="respond", payload={"query": request}))
        await params.result_callback(
            "Handed to the window; it will answer aloud.",
            properties=FunctionCallResultProperties(run_llm=False),
        )

    async def _past_searches(self, params: FunctionCallParams):
        if self._store is None:
            await params.result_callback({"error": "no memory of past searches here"})
            return
        query = str(params.arguments.get("query") or "")
        asks = await self._store.search_asks(query, limit=5)
        if asks and self._on_show_ask:
            # The window jumps to the best match while it is read back.
            self._on_show_ask(asks[0].id)
        await params.result_callback(
            {
                "now": datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M"),
                "searches": [
                    {
                        "when": datetime.fromtimestamp(a.ts).astimezone().strftime("%A %Y-%m-%d %H:%M"),
                        "question": a.question,
                        "answer": a.answer,
                        "asked": "by voice" if a.source == "voice" else "typed",
                    }
                    for a in asks
                ],
            }
        )

    async def _show_me(self, params: FunctionCallParams):
        if not self._last_ids or self._on_show is None:
            await params.result_callback({"opened": False, "reason": "nothing to show yet"})
            return
        self._on_show(list(self._last_ids))
        await params.result_callback({"opened": True, "count": len(self._last_ids)})

    async def _list_windows(self, params: FunctionCallParams):
        if self._registry is None:
            await params.result_callback({"windows": [], "note": "Only the shared screen is available."})
            return
        from macos.registry import collapse_tabs

        app = str(params.arguments.get("app") or "").strip().lower()
        windows = [
            {"app": w.app, "title": w.title, "on_screen": w.on_screen, **({"tabs": list(w.tabs)} if w.tabs else {})}
            for w in collapse_tabs(self._registry.windows)
            if w.title.strip() and (not app or app in w.app.lower())
        ]
        await params.result_callback({"windows": windows[:MAX_LISTED_WINDOWS]})

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
        hit = update.get("hit")
        if hit:
            # From a watcher restored at startup, delivered on the subscribe job.
            self._moments.enqueue(Moment(kind=MomentKind.WATCH, text=hit))
            return
        warning = update.get("warning")
        if warning:
            # A watched window went out of sight, or came back. Spoken for
            # now; once the ui worker exists these become banners by default.
            self._moments.enqueue(Moment(kind=MomentKind.WARNING, text=warning))
            return
        text = update.get("say")
        if text:
            self._moments.enqueue(Moment(kind=self._kind_for(message.job_id), text=text))

    async def on_job_response(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_response(message)
        self._look_jobs.discard(message.job_id)
        self._watch_jobs.discard(message.job_id)
        question = self._look_questions.pop(message.job_id, None)
        if message.status == JobStatus.CANCELLED:
            return
        response = message.response or {}
        if response.get("observation_ids"):
            self._last_ids = [int(i) for i in response["observation_ids"]]
        text = response.get("answer")
        if text:
            self._moments.enqueue(Moment(kind=MomentKind.ANSWER, text=text))
            if question is not None and self._on_answer:
                # The window shows what is being said and the frames behind it.
                await self._on_answer(question, text, list(self._last_ids))

    async def on_job_error(self, message: BusJobResponseMessage | BusJobResponseUrgentMessage):
        await super().on_job_error(message)
        self._look_jobs.discard(message.job_id)
        self._watch_jobs.discard(message.job_id)
        logger.warning(f"{self}: job {message.job_id} failed: {message.response}")
        text = (message.response or {}).get("answer") or "Sorry, I couldn't check that."
        self._moments.enqueue(Moment(kind=MomentKind.ANSWER, text=text))
