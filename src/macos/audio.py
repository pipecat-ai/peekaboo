#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The native macOS audio transport: ``AVAudioEngine`` with voice processing.

One engine carries both directions so the OS echo canceller sees what the
speakers play: a tap on the input node feeds the pipeline, a player node on
the same engine plays what the pipeline says. Voice-processing I/O adds echo
cancellation, gain control, and noise suppression, which is what lets an
always-on microphone sit next to the speakers without the bot hearing
itself. Measured in M0: the bot's own speech goes from 23 dB above the quiet
floor to 21 dB below it.

Order of operations, which the engine is strict about: build the output
graph, enable voice processing, install the tap, start. Enabled first, the
output node ends up with a 0 Hz format and the engine fails with -10875.

Formats: the engine runs at the hardware rate (48 kHz here) and the tap asks
for mono float32 at that rate, which is resampled to the pipeline's input
rate. Output is scheduled as int16 mono at the pipeline's output rate; the
mixer converts. The tap block runs on the audio thread and does nothing but
copy bytes and hop into the asyncio loop.
"""

import asyncio
from collections.abc import Callable
from typing import Optional

import AVFoundation as AVF
import numpy as np
from Foundation import NSNotificationCenter
from loguru import logger
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

# How far ahead of the play head output is scheduled before the writer waits.
# Small keeps interruptions snappy; large survives a busy event loop.
OUTPUT_LEAD_SECS = 0.12

# Tap buffer length asked for. The engine clamps it to 100 ms whatever we
# say, so buffers are sliced to this length on the way into the pipeline.
TAP_BUFFER_SECS = 0.02

DEFAULT_OUTPUT_RATE = 24000


class MacAudioTransportParams(TransportParams):
    """Parameters for the macOS audio transport.

    Parameters:
        voice_processing: Enable the OS echo canceller, gain control, and
            noise suppression. Off only for measurement.
    """

    voice_processing: bool = True


class _Engine:
    """The shared ``AVAudioEngine`` and its graph.

    Built once both sides know their rates, started once, and rebuilt in
    place when the system reports a configuration change (a new default
    device), which stops the engine and may change the hardware rate.
    """

    def __init__(self, *, voice_processing: bool):
        self._voice_processing = voice_processing
        self._engine = AVF.AVAudioEngine.alloc().init()
        self._player = None
        self._out_format = None
        self._out_rate = DEFAULT_OUTPUT_RATE
        self._in_rate = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_input: Optional[Callable[[bytes, int], None]] = None
        self._observer = None
        self._built = False
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def input_rate(self) -> int:
        return self._in_rate

    def set_output_rate(self, rate: int):
        self._out_rate = rate

    def set_input_handler(self, loop: asyncio.AbstractEventLoop, on_input: Callable[[bytes, int], None]):
        self._loop = loop
        self._on_input = on_input

    def start(self):
        if self._running:
            return
        if not self._built:
            self._build()
        self._engine.prepare()
        ok, error = self._engine.startAndReturnError_(None)
        if not ok:
            raise RuntimeError(f"AVAudioEngine failed to start: {error}")
        self._player.play()
        self._running = True
        logger.info(
            f"audio engine running: input {self._in_rate} Hz, output {self._out_rate} Hz, "
            f"voice processing {'on' if self._engine.inputNode().isVoiceProcessingEnabled() else 'off'}"
        )

    def stop(self):
        if not self._built:
            return
        if self._observer is not None:
            NSNotificationCenter.defaultCenter().removeObserver_(self._observer)
            self._observer = None
        self._engine.inputNode().removeTapOnBus_(0)
        self._engine.stop()
        self._running = False

    def _build(self):
        engine = self._engine
        input_node = engine.inputNode()

        # 1. The output graph: a player of int16 mono at the pipeline's rate.
        self._player = AVF.AVAudioPlayerNode.alloc().init()
        self._out_format = AVF.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
            AVF.AVAudioPCMFormatInt16, float(self._out_rate), 1, False
        )
        engine.attachNode_(self._player)
        engine.connect_to_format_(self._player, engine.mainMixerNode(), self._out_format)

        # 2. Voice processing, now that the output side exists.
        if self._voice_processing:
            ok, error = input_node.setVoiceProcessingEnabled_error_(True, None)
            if not ok:
                logger.warning(f"voice processing could not be enabled: {error}")

        # 3. The tap.
        self._install_tap()

        # 4. A new default device stops the engine; pick it back up.
        self._observer = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AVF.AVAudioEngineConfigurationChangeNotification, engine, None, self._on_configuration_change
        )
        self._built = True

    def _install_tap(self):
        input_node = self._engine.inputNode()
        hardware = input_node.outputFormatForBus_(0)
        rate = int(hardware.sampleRate())
        if rate <= 0:
            raise RuntimeError("no audio input device")
        self._in_rate = rate

        # Mono float32 at the hardware rate. With voice processing on the
        # node reports nine identical channels; asking for one works.
        tap_format = AVF.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(float(rate), 1)
        loop, on_input = self._loop, self._on_input

        def tap(buffer, when):
            n = buffer.frameLength()
            data = buffer.floatChannelData()
            if n == 0 or data is None or loop is None or on_input is None:
                return
            pcm = bytes(data[0].as_buffer(n))
            loop.call_soon_threadsafe(on_input, pcm, rate)

        input_node.installTapOnBus_bufferSize_format_block_(0, int(rate * TAP_BUFFER_SECS), tap_format, tap)

    def _on_configuration_change(self, notification):
        if self._loop:
            self._loop.call_soon_threadsafe(self._reconfigure)

    def _reconfigure(self):
        """The default device changed. The engine has stopped; the hardware
        rate may differ. Reinstall the tap and start again."""
        was_rate = self._in_rate
        self._running = False
        try:
            self._engine.inputNode().removeTapOnBus_(0)
            self._install_tap()
            self._engine.prepare()
            ok, error = self._engine.startAndReturnError_(None)
            if not ok:
                logger.error(f"audio engine did not restart after device change: {error}")
                return
            self._player.play()
            self._running = True
            logger.info(f"audio device changed: input {was_rate} -> {self._in_rate} Hz, engine restarted")
        except Exception as e:  # noqa: BLE001 - report, keep the pipeline alive
            logger.error(f"audio engine reconfiguration failed: {e}")

    def schedule(self, pcm: bytes) -> float:
        """Queue int16 mono output. Returns its duration in seconds."""
        frames = len(pcm) // 2
        if frames == 0 or not self._running:
            return 0.0
        buffer = AVF.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(self._out_format, frames)
        buffer.setFrameLength_(frames)
        buffer.int16ChannelData()[0].as_buffer(frames)[:] = pcm
        self._player.scheduleBuffer_completionHandler_(buffer, None)
        return frames / self._out_rate

    def flush(self):
        """Drop everything queued for playback."""
        if self._player and self._running:
            self._player.stop()
            self._player.play()


class MacAudioInputTransport(BaseInputTransport):
    """Microphone in: tap buffers, float32 at the hardware rate, resampled to
    the pipeline's rate as 16-bit PCM."""

    _params: MacAudioTransportParams

    def __init__(self, engine: _Engine, params: MacAudioTransportParams, *, on_ready: Callable):
        super().__init__(params)
        self._engine = engine
        self._on_ready = on_ready
        self._resampler = create_stream_resampler()
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        self._engine.set_input_handler(self.get_event_loop(), self._on_audio)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._queue = asyncio.Queue()
        self._task = self.create_task(self._drain(), name="mic")
        self._engine.start()
        await self.set_transport_ready(frame)
        await self._on_ready()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._stop_drain()
        self._engine.stop()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._stop_drain()
        self._engine.stop()

    async def _stop_drain(self):
        if self._task:
            task, self._task = self._task, None
            await self.cancel_task(task)

    def _on_audio(self, pcm: bytes, rate: int):
        # On the loop, called from the tap via call_soon_threadsafe.
        if self._queue:
            self._queue.put_nowait((pcm, rate))

    async def _drain(self):
        chunk_bytes = int(self.sample_rate * TAP_BUFFER_SECS) * 2
        while True:
            pcm, rate = await self._queue.get()
            samples = np.clip(np.frombuffer(pcm, dtype=np.float32), -1.0, 1.0)
            audio = (samples * 32767.0).astype(np.int16).tobytes()
            if rate != self.sample_rate:
                audio = await self._resampler.resample(audio, rate, self.sample_rate)
            # The tap hands over 100 ms at a time; the pipeline wants 20 ms.
            for start in range(0, len(audio), chunk_bytes):
                await self.push_audio_frame(
                    InputAudioRawFrame(
                        audio=audio[start : start + chunk_bytes],
                        sample_rate=self.sample_rate,
                        num_channels=1,
                    )
                )


class MacAudioOutputTransport(BaseOutputTransport):
    """Speaker out: pipeline audio scheduled on the player node, paced so no
    more than a short lead is queued, and flushed on interruption."""

    _params: MacAudioTransportParams

    def __init__(self, engine: _Engine, params: MacAudioTransportParams):
        super().__init__(params)
        self._engine = engine
        self._play_head = 0.0

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        self._engine.set_output_rate(self.sample_rate)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._engine.start()
        await self.set_transport_ready(frame)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, InterruptionFrame):
            # Whatever is queued on the device is stale now.
            self._engine.flush()
            self._play_head = 0.0
        await super().process_frame(frame, direction)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        if not self._engine.running:
            return False
        if frame.num_channels != 1:
            audio = np.frombuffer(frame.audio, dtype=np.int16).reshape(-1, frame.num_channels)
            pcm = audio.mean(axis=1).astype(np.int16).tobytes()
        else:
            pcm = frame.audio
        duration = self._engine.schedule(pcm)

        # Scheduling returns at once; pace like a blocking device would, so
        # the pipeline's notion of "speaking" tracks the speakers.
        now = self.get_event_loop().time()
        self._play_head = max(self._play_head, now) + duration
        ahead = self._play_head - now
        if ahead > OUTPUT_LEAD_SECS:
            await asyncio.sleep(ahead - OUTPUT_LEAD_SECS)
        return True


class MacAudioTransport(BaseTransport):
    """Local audio on macOS through one ``AVAudioEngine``.

    Events:
        on_ready: The engine is running and the pipeline has started.
    """

    def __init__(self, params: Optional[MacAudioTransportParams] = None):
        super().__init__()
        self._params = params or MacAudioTransportParams(audio_in_enabled=True, audio_out_enabled=True)
        self._engine = _Engine(voice_processing=self._params.voice_processing)
        self._input: Optional[MacAudioInputTransport] = None
        self._output: Optional[MacAudioOutputTransport] = None
        self._register_event_handler("on_ready")

    def input(self) -> FrameProcessor:
        if not self._input:
            self._input = MacAudioInputTransport(self._engine, self._params, on_ready=self._ready)
        return self._input

    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = MacAudioOutputTransport(self._engine, self._params)
        return self._output

    async def _ready(self):
        await self._call_event_handler("on_ready")
