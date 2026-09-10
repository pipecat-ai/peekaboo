#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import hashlib
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
from pipecat.services.llm_service import LLMService
from pipecat.workers.llm.llm_worker import LLMWorker
from pipecat.workers.llm.tool_decorator import tool
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.services.cartesia.tts import CartesiaHttpTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.moonshine.stt import MoonshineSTTService
from pipecat.transports.base_transport import BaseTransport

from moments import Moment, MomentKind, MomentPolicy
from processors.conversation import ConversationState
import models
from processors.wake import LocalTranscriptionFrame, WakeGate
from processors.screen_bridge import ScreenBridge
from workers.names import HISTORY_WORKER, SCREEN_WORKER, UI_WORKER, VISION_WORKER, VOICE_WORKER

if TYPE_CHECKING:
    from store.sqlite_store import SQLiteStore
    from macos.registry import WindowRegistry

# A meeting reminder nobody acted on stops mattering a while after the start.
MEETING_MOMENT_LIFETIME_SECS = 20 * 60

# The voice LLM only routes: call a tool or not, and phrase short lines. The
# fastest tier is the right one; a slower model here is felt on every turn.

# Spoken the moment a screen question goes out, instead of a second LLM call
# to phrase an acknowledgement. Saves about 1.3 s on every question.
LOOK_FILLER = "One moment."
REMEMBER_FILLER = "Let me think back."
WATCH_FILLER = "I'll let you know."

# Spoken on launch. The first time a voice says it the audio is kept as a WAV
# under the store, and every launch after that plays the file: no TTS call.
GREETING = "Welcome to Peekaboo."
# The first run, with no API key yet: the window opens on Settings meanwhile.
ONBOARDING_GREETING = (
    "Welcome to Peekaboo. It looks like this is the first time you run me. "
    "Let's go to Settings, where you can enter your API key. Once it's saved, restart me and we're ready."
)

# list_windows is read to a model, not a person; keep it short.
MAX_LISTED_WINDOWS = 25

# Where speech runs. Recognition always starts on the machine: Moonshine hears
# everything and only what follows the wake phrase goes further. "cloud"
# speaks through Cartesia over HTTP, a request per utterance, so nothing is
# connected while idle. "local" stays on the machine throughout, with Kokoro
# speaking.
SpeechServices = Literal["cloud", "local"]

# Who hears the conversation once awake. "moonshine": the same local
# recognizer, a segment per utterance. "deepgram": a streaming recognizer
# connected on wake and dropped on sleep, kept for comparison.
Recognizer = Literal["moonshine", "deepgram"]

# Spoken when the wake phrase comes alone.
WAKE_ACK = "Yes?"

# Silence that ends an utterance. Long enough that "Peekaboo," and the
# question after it are heard as one, short enough not to drag the turn end.
WAKE_VAD_STOP_SECS = 0.6

# Cartesia voice when CARTESIA_VOICE_ID is not set: British Reading Lady.
CARTESIA_VOICE = "71a7ad14-091c-4e8e-a314-022ece01c121"


# Kokoro on the CPU synthesizes a long paragraph in one go and hands it over
# whole, so a TTS context can sit silent for a few seconds before its audio
# arrives; the default 3 s idle timeout closes it as silent. Answers are spoken
# sentence by sentence to keep each context short; this is the backstop.
TTS_IDLE_TIMEOUT_SECS = 15.0

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")

SYSTEM_INSTRUCTION = """

You are a voice assistant on the user's Mac. You cannot see the screen yourself
and you have no memory of it; your tools do. [look] sees the screen or one
window right now. [remember] searches the record of everything that was on
screen, going back days. [watch] tells the user when something happens on
the screen or in one window. Answers and watch hits are delivered to the
user separately, after your turn.

Targets: when the user names a window or an app ("the terminal", "Chrome",
"the build window"), pass what they said as the target. Leave the target empty
to watch every window and notification. [list_windows] tells you what is open.

Tool-use rules:

- If the user asks a general knowledge question, DO NOT call any tool.

- Say times the way a person would: "ten fifteen in the morning", "a quarter
  past three in the afternoon", "around five PM"; never seconds, never
  24-hour or ISO forms, and no date when it is today.

- If the user asks about what is on the screen or in a window now ("what
  does the terminal say", "is the build done", "what's in my inbox"), call
  [look]. If they ask about the past, anything before this moment ("what
  was I doing this morning", "what PR did I look at yesterday", "what did
  that error say earlier"), call [remember]. Never say you have no access
  to the past; the tool does. NEVER answer the question yourself.

- If the user wants to be told when something happens, call [watch] with the
  condition in their words. To stop, call [unwatch]. [list_watchers] says
  what is being watched.

- When you call [look], [remember] or [watch], call it without saying anything first. An
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
  [list_watchers]. "Open the window", "show the main window", "open your
  window", "show yourself", "open Peekaboo" mean the Peekaboo window, the
  app's own: call [window] with "open the window"; it is never one of the
  user's app windows, so do not call [list_windows] for it. "This image", "this screenshot", "this one", "what is
  this about", "what was this" refer to the memory open in the window: call
  [window], never [look]. Never call [look] with Peekaboo as the target;
  Peekaboo's own window is never the subject of a look. Anything about
  clicking, opening, selecting, taking a look at, or seeing details of an
  image, screenshot, block or memory is for [window], including follow-ups
  such as "can we take a look", "click on that", "open it", "more details":
  never answer those yourself, never say you cannot click. That includes
  questions about what the Timeline shows: a block, an hour, a selection,
  "the last block around three", "what was I doing in that one". Those are
  about what is on the window, not a search of the past; [remember] is for
  the past when nothing on the window is being pointed at. Moving in time is
  window navigation too: "go to last Friday", "can we go to yesterday",
  "show me three PM", "jump to the 14th", "next day", "back a week" mean
  [window] with their words; the window knows what day it is and moves the
  Timeline. Only a question about what happened ("what was I doing last
  Friday") is a [remember]. After a window
  request, a bare follow-up is still for [window]: "the third", "just open
  it", "no, the other one", "I meant the second". You cannot see the window:
  never say what it shows or that something is already open; hand it over.

- "Start recording", "pause recording", "stop recording" mean call
  [set_recording]. Recording is what builds the memory of the screen.

- Reminders about meetings or scheduled events that appeared on the screen
  arrive as developer messages. Tell the user in one short sentence and, if
  there is a link, ask whether to open it. When they agree, call
  [join_meeting]. If they ask for later, call [snooze_reminder]. Never read a
  URL aloud.

- Act on the likeliest reading of what the user said, and acknowledge in a
  word or two: "Sure.", "You got it.", "Here you go.", "On it." Ask a
  question only when you cannot tell what they mean at all; never ask which
  screen or which window when they asked for the Peekaboo window: "open the
  window", "show me Peekaboo", "open the main window" mean [window] with
  "open the window", right away, and the window opens where it was.

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


class VoiceWorker(LLMWorker):
    """The conversation: transport, STT, LLM, TTS.

    A Pipecat ``LLMWorker`` around the transport pipeline: the ``@tool``
    methods below are the conversation's tools, their schemas read from their
    signatures and docstrings. Screen questions become ``look`` jobs on the
    vision worker and watch requests become ``watch`` jobs on the screen
    worker, each carrying the window or app the user named. Whatever comes back, an answer, a progress
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
        history_worker: str = HISTORY_WORKER,
        ui_worker: str = UI_WORKER,
        screen_from_transport: bool = True,
        open_links: bool = True,
        speech: SpeechServices = "local",
        stt: Recognizer = "moonshine",
        stt_model: Optional[str] = None,
        tts_voice: Optional[str] = None,
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
    ):
        self._transport = transport
        self._vision_worker = vision_worker
        self._screen_worker = screen_worker
        self._history_worker = history_worker
        self._ui_worker = ui_worker
        self._open_links = open_links
        self._speech = speech
        self._stt = stt
        # A Moonshine model name (see ``pipecat.services.moonshine.stt.Model``); None is the service default.
        self._stt_model = stt_model or models.current().stt_model
        # The Kokoro voice; the greeting cache is per voice.
        self._tts_voice = tts_voice or models.current().tts_voice
        # The window registry; BaseWorker owns ``_registry`` (the worker registry).
        self._windows = registry
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

        llm, pipeline = self._build_pipeline()

        # Active from the start: activation is what sets the tools. Frames a
        # tool queues go out at once: the acknowledgement ("One moment.") is
        # spoken while the job it started runs, not after the tool returns.
        super().__init__(VOICE_WORKER, llm=llm, pipeline=pipeline, active=True, defer_tool_frames=False)

    def _speech_services(self) -> list[FrameProcessor]:
        """The recognition stage and the synthesizer: ``[*stt, tts]``.

        Recognition is Moonshine on the machine, always, hearing everything
        (its transcripts are what the wake gate reads) and, unless Deepgram
        is chosen, the conversation too. With Deepgram, it follows Moonshine,
        connected only while the gate is awake. Cartesia speaks over HTTP in
        cloud mode; in local mode Kokoro speaks and nothing is ever connected.
        """
        # Audio passes through Moonshine: the user aggregator's VAD and turn
        # detection run on what reaches it, and a cloud recognizer behind
        # Moonshine hears through it too, once awake. The gate comes last and
        # sees every recognizer's transcripts.
        cloud_stt = self._stt == "deepgram"
        moonshine_settings = MoonshineSTTService.Settings(model=self._stt_model) if self._stt_model else None
        stage: list[FrameProcessor] = [LocalMoonshineSTTService(settings=moonshine_settings, audio_passthrough=True)]
        if cloud_stt:
            self._cloud_stt = OnDemandDeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
            stage.append(self._cloud_stt)
        if self._speech == "local":
            from pipecat.services.kokoro.tts import KokoroTTSService

            tts = KokoroTTSService(
                settings=KokoroTTSService.Settings(voice=self._tts_voice),
                stop_frame_timeout_s=TTS_IDLE_TIMEOUT_SECS,
            )
        else:
            tts = CartesiaHttpTTSService(
                api_key=os.getenv("CARTESIA_API_KEY"),
                settings=CartesiaHttpTTSService.Settings(voice=os.getenv("CARTESIA_VOICE_ID") or CARTESIA_VOICE),
            )
        if self._wake_word:
            self._wake_gate = WakeGate(
                on_wake=self._on_wake,
                on_sleep=self._on_sleep,
                on_acknowledge=lambda: self.say(WAKE_ACK),
                local_conversation=not cloud_stt,
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
            return f"kokoro-{self._tts_voice}"
        return f"cartesia-{os.getenv('CARTESIA_VOICE_ID') or CARTESIA_VOICE}"

    def _build_pipeline(self) -> tuple[LLMService, Pipeline]:
        *stt, tts = self._speech_services()

        llm = models.make_llm(
            models.current().voice,
            name="VoiceLLMService",
            system_instruction=SYSTEM_INSTRUCTION,
            retry_on_timeout=True,
        )
        context = LLMContext()
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

        return llm, Pipeline(processors)

    async def start_session(self, client_id: str, *, recording: bool = True, greeting: Optional[str] = None):
        """Kick off the conversation once the client is connected.

        Call after screen capture is enabled on the transport. With
        ``recording`` false the screen worker is left idle until asked.
        ``greeting`` replaces the fixed opening line (the first run says
        where the API key goes).
        """
        if self._screen_bridge:
            self._screen_bridge.set_client_id(client_id)
        self._recording = recording

        # A fresh conversation per connection, opened with a fixed line and no
        # LLM call; from the cache when this voice has said it before.
        await self.queue_frame(LLMMessagesUpdateFrame(messages=[], run_llm=False))
        await self._greet(greeting or GREETING)

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

    async def _greet(self, text: str = GREETING):
        # Cached per voice and per line: the onboarding line is not the usual one.
        suffix = "" if text == GREETING else f"-{hashlib.sha1(text.encode()).hexdigest()[:8]}"
        cached = self._greeting_cache / f"{self._voice_key()}{suffix}.wav" if self._greeting_cache else None
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
        await self.say(text)

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

    @tool
    async def look(self, params: FunctionCallParams, question: str, target: Optional[str] = None):
        """Answer a question about the screen or one window: what is visible now, or what was on screen earlier today or on a past day. UI, text, errors, numbers, links, anything the user looked at.

        Args:
            question: The exact question the user is asking.
            target: The window or app the user named, in their words: 'the terminal', 'Chrome', part of a window title. Empty for the whole screen.
        """
        target = target or ""

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

    @tool
    async def remember(self, params: FunctionCallParams, question: str):
        """Answer a question about the past from the record of what was on screen: earlier today or on a past day. What the user was doing, what a window said, what they looked at.

        Args:
            question: The exact question the user is asking.
        """
        # Straight to the history worker: no picture, no vision model. The
        # answer comes back as a job message and is spoken then.
        job_id = await self.request_job(self._history_worker, params=JobParams(name="search", payload={"query": question}))
        self._look_jobs.add(job_id)
        self._look_questions[job_id] = question
        if self._on_asked:
            self._on_asked(question)
        await self._acknowledge(params, REMEMBER_FILLER)

    @tool
    async def watch(self, params: FunctionCallParams, condition: str, target: Optional[str] = None):
        """Tell the user when something happens on the screen or in one window: a build finishing, a message arriving, a value changing, a window appearing.

        Args:
            condition: What to watch for, in the user's words.
            target: The window or app the user named, in their words: 'the terminal', 'Chrome', part of a window title. Empty to watch every window and notification.
        """
        await self._start_watch(condition, target or "")
        await self._acknowledge(params, WATCH_FILLER)

    async def _start_watch(self, condition: str, target: str, wanted: Optional[str] = None) -> str:
        job_id = await self.request_job(
            self._screen_worker,
            params=JobParams(name="watch", payload={"query": condition, "target": target, "wanted": wanted or target}),
        )
        self._watch_jobs.add(job_id)
        return job_id

    @job(name="watch")
    async def _watch_job(self, message: BusJobRequestMessage):
        """A watcher asked for from the app rather than aloud. Created here so
        its hits become spoken moments like any other."""
        payload = message.payload or {}
        job_id = await self._start_watch(
            str(payload.get("condition", "")), str(payload.get("target") or ""), str(payload.get("wanted") or "") or None
        )
        await self.send_job_response(message.job_id, {"started": True, "job_id": job_id})

    async def _acknowledge(self, params: FunctionCallParams, filler: str):
        await self.say(filler)
        await params.result_callback(
            f"Acknowledged to the user with: \"{filler}\" The result will be spoken separately.",
            properties=FunctionCallResultProperties(run_llm=False),
        )

    @tool
    async def unwatch(self, params: FunctionCallParams, id: Optional[int] = None, target: Optional[str] = None):
        """Stop watching. Give the watcher id from list_watchers, or the target to stop every watcher on it, or nothing to stop them all.

        Args:
            id: A watcher id from list_watchers.
            target: The window or app the user named, to stop every watcher on it.
        """
        payload = {}
        if id is not None:
            payload["id"] = int(id)
        if target:
            payload["target"] = str(target)
        try:
            async with self.job(self._screen_worker, params=JobParams(name="unwatch", payload=payload)) as t:
                pass
        except JobError as e:
            await params.result_callback({"error": str(e)})
            return
        await params.result_callback(t.response or {})

    @tool
    async def list_watchers(self, params: FunctionCallParams):
        """What is currently being watched, with ids, to say it aloud when the user asks what is being watched. Not for showing the Watchers screen: that is [window]."""
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

    @tool
    async def set_recording(self, params: FunctionCallParams, on: bool):
        """Start or pause recording the screen into memory.

        Args:
            on: True to record, false to pause.
        """
        on = bool(on)
        self._recording = on
        if self._on_recording is not None:
            await self._on_recording(on)
        else:
            await self.request_job(
                self._screen_worker,
                params=JobParams(name="capture", payload={"action": "start" if on else "stop"}),
            )
        await params.result_callback({"recording": on})

    @tool
    async def window(self, params: FunctionCallParams, request: str):
        """Operate the Peekaboo window, or answer about what it shows. Call it when the user refers to the window or something on it: open/show/click/select/go back, 'the first one', 'the third screenshot', 'this one', 'take a look', 'more details', switch to the timeline, searches, watchers or settings, or asks what one of the shown memories is about. The window can click anything it shows.

        Args:
            request: What the user wants, in their words.
        """
        # The ui worker sees the window's accessibility snapshot, acts on the
        # page, and speaks its own short reply through this pipeline's TTS.
        # Asking the window for something means wanting to see it.
        if self._on_open_window:
            self._on_open_window()
        request = str(request or "")
        await self.request_job(self._ui_worker, params=JobParams(name="respond", payload={"query": request}))
        await params.result_callback(
            "Handed to the window; it will answer aloud.",
            properties=FunctionCallResultProperties(run_llm=False),
        )

    @tool
    async def past_searches(self, params: FunctionCallParams, query: str):
        """Find questions the user asked before and what was answered. Call it when the user refers to an earlier question or search: 'what did I ask yesterday', 'I remember searching for...', 'what did you tell me about...'.

        Args:
            query: A few words from the earlier question or its answer. Empty for the most recent ones.
        """
        if self._store is None:
            await params.result_callback({"error": "no memory of past searches here"})
            return
        query = str(query or "")
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

    @tool
    async def show_me(self, params: FunctionCallParams):
        """Open the memories window on the results behind the last answer: the answer and the memories it drew on. Call it when the user says 'show me' or asks to see it; opening one of them is then a request for [window]."""
        if not self._last_ids or self._on_show is None:
            await params.result_callback({"opened": False, "reason": "nothing to show yet"})
            return
        self._on_show(list(self._last_ids))
        await params.result_callback({"opened": True, "count": len(self._last_ids)})

    @tool
    async def list_windows(self, params: FunctionCallParams, app: Optional[str] = None):
        """The apps and windows open right now, front to back.

        Args:
            app: Only this app's windows. Empty for all.
        """
        if self._windows is None:
            await params.result_callback({"windows": [], "note": "Only the shared screen is available."})
            return
        from macos.registry import collapse_tabs

        app = str(app or "").strip().lower()
        windows = [
            {"app": w.app, "title": w.title, "on_screen": w.on_screen, **({"tabs": list(w.tabs)} if w.tabs else {})}
            for w in collapse_tabs(self._windows.windows)
            if w.title.strip() and (not app or app in w.app.lower())
        ]
        await params.result_callback({"windows": windows[:MAX_LISTED_WINDOWS]})

    @tool
    async def join_meeting(self, params: FunctionCallParams):
        """Open the join link of the meeting that was just announced. Call it when the user agrees to join or open the meeting."""
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

    @tool
    async def snooze_reminder(self, params: FunctionCallParams, minutes: Optional[int] = None):
        """Remind the user about the announced meeting again in a few minutes.

        Args:
            minutes: How many minutes from now to remind again. Defaults to 5.
        """
        meeting = self._moments.last_meeting
        if not meeting:
            await params.result_callback({"snoozed": False, "reason": "no reminder to snooze"})
            return
        minutes = int(minutes or 5)
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
