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
import math
import time
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
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from macos.audio_devices import (
    InputDevice,
    create_aggregate,
    default_input_id,
    default_output,
    destroy_aggregate,
    input_device_id,
    input_devices,
    pin_input_unit,
    same_headset,
)

# How far ahead of the play head output is scheduled before the writer waits.
# Small keeps interruptions snappy; large survives a busy event loop.
OUTPUT_LEAD_SECS = 0.12

# Tap buffer length asked for. The engine clamps it to 100 ms whatever we
# say, so buffers are sliced to this length on the way into the pipeline.
TAP_BUFFER_SECS = 0.02

DEFAULT_OUTPUT_RATE = 24000

# How often the input level is written to the log.
MIC_LEVEL_LOG_SECS = 5.0

# Starting the engine is retried this often while the input unit settles.
START_ATTEMPTS = 8
START_RETRY_SECS = 0.25


class MacAudioTransportParams(TransportParams):
    """Parameters for the macOS audio transport.

    Parameters:
        voice_processing: Enable the OS echo canceller, gain control, and
            noise suppression. Off only for measurement.
        input_device: UID of the microphone to use (see
            :func:`macos.audio_devices.input_devices`); "" follows the
            system default.
    """

    voice_processing: bool = True
    input_device: str = ""


class _Engine:
    """The shared ``AVAudioEngine`` and its graph.

    Built once both sides know their rates, started once, and rebuilt in
    place when the system reports a configuration change (a new default
    device), which stops the engine and may change the hardware rate.
    """

    def __init__(self, *, voice_processing: bool, input_device: str = ""):
        self._voice_processing = voice_processing
        self._input_device = input_device
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
        # A private aggregate (output + chosen microphone) and what it was made for.
        self._aggregate: Optional[int] = None
        self._aggregate_for: tuple[str, str] = ("", "")
        self._vp_active = False
        self._tap_installed = False

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
        """Start now; raises if the engine will not start."""
        if self._running:
            return
        if not self._built:
            self._build()
        error = self._try_start()
        if error is not None:
            raise RuntimeError(f"AVAudioEngine failed to start: {error}")

    async def start_async(self):
        """Start, giving the engine time to settle first if it needs it.

        Pinning a microphone after voice processing makes the input unit
        reconfigure itself: the engine posts a configuration change and
        refuses to start (-10875) until that is handled, which happens on
        this loop (:meth:`_reconfigure`, which starts the engine itself).
        Blocking here would hold that up, so each attempt yields.
        """
        if self._running:
            return
        if not self._built:
            self._build()
        error = None
        for _ in range(START_ATTEMPTS):
            if self._running:
                return
            error = self._try_start()
            if error is None:
                return
            logger.debug(f"audio engine did not start ({error}); waiting for it to settle")
            await asyncio.sleep(START_RETRY_SECS)
        if not self._running:
            raise RuntimeError(f"AVAudioEngine failed to start: {error}")

    def _try_start(self):
        """One attempt; None on success, else the error."""
        self._engine.prepare()
        ok, error = self._engine.startAndReturnError_(None)
        if not ok:
            return error
        if not self._tap_installed:
            self._install_tap()
        self._player.play()
        self._running = True
        logger.info(
            f"audio engine running: input {self._in_rate} Hz, output {self._out_rate} Hz, "
            f"voice processing {'on' if self._engine.inputNode().isVoiceProcessingEnabled() else 'off'}"
        )

    def set_input_device(self, uid: str):
        """Switch microphones while running: "" for the system default.

        The input unit's device can only be set before the engine starts,
        and there is no unsetting it, so the engine is built again from
        scratch either way.
        """
        if uid == self._input_device:
            return
        self._input_device = uid
        if self._built:
            self._rebuild()

    def set_voice_processing(self, enabled: bool):
        """Turn the OS voice processing on or off while running.

        Voice-processing I/O is system-wide: while any process has it on,
        other apps capturing the same microphone get the ducked, processed
        path (a screen recorder's voice track comes out muffled or silent).
        Off, Peekaboo may hear itself through the speakers; headphones
        avoid that. The engine has to be stopped to change the mode, and the
        tap is reinstalled since the input format may change with it.
        """
        if enabled == self._voice_processing:
            return
        self._voice_processing = enabled
        if self._built:
            self._rebuild()

    def stop(self):
        if not self._built:
            return
        if self._observer is not None:
            NSNotificationCenter.defaultCenter().removeObserver_(self._observer)
            self._observer = None
        self._remove_tap()
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

        # 2. Voice processing, now that the output side exists, unless it
        # would take the microphone away (see _voice_processing_allowed).
        # With it on, macOS picks the microphone itself.
        self._vp_active = False
        if self._voice_processing_allowed():
            ok, error = input_node.setVoiceProcessingEnabled_error_(True, None)
            if not ok:
                logger.warning(f"voice processing could not be enabled: {error}")
            self._vp_active = bool(ok)
        else:
            # 3. A chosen microphone. The input and output nodes share one
            # I/O unit, so that unit gets an aggregate of the output device
            # and the microphone; the microphone alone would leave the
            # output with no device and the engine unable to start.
            self._pin_input(input_node)

        # 4. The tap. With voice processing on it must be in place before
        # the engine starts (the format is not writable afterwards); with a
        # pinned microphone it can only come after, once the unit has
        # settled on the new device (see _try_start).
        if self._vp_active:
            self._install_tap()

        # 5. A new default device, or the unit reconfiguring itself after a
        # pin, stops the engine; pick it back up with a fresh tap.
        self._observer = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AVF.AVAudioEngineConfigurationChangeNotification, engine, None, self._on_configuration_change
        )
        self._built = True

    def _voice_processing_allowed(self) -> bool:
        """Voice processing, when wanted, unless the user chose a microphone
        it would override.

        Voice-processing I/O is one unit for both directions and picks its
        own input: with a Bluetooth headset as the output it insists on the
        headset's microphone, and rejects any other device set on the unit,
        an aggregate included (measured). So a chosen microphone that is not
        that headset's wins, and the echo canceller stays off for this
        engine; with headphones on there is nothing to cancel.
        """
        if not self._voice_processing:
            return False
        if not self._input_device:
            return True
        out = default_output()
        if out is None or out.transport != "Bluetooth" or same_headset(self._input_device, out.uid):
            return True
        logger.info(
            f"voice processing off for now: with {out.name} as the output it would use that headset's "
            f"microphone instead of {self._input_device!r}"
        )
        return False

    def _pin_input(self, input_node):
        if not self._input_device:
            return
        if input_device_id(self._input_device) is None:
            logger.warning(f"input device {self._input_device!r} is not present; using the system default")
            return
        if input_device_id(self._input_device) == default_input_id():
            # The engine follows the system default on its own.
            logger.info(f"microphone: {self._input_device!r} (the system default)")
            return
        out = default_output()
        if out is None:
            return
        wanted = (out.uid, self._input_device)
        if self._aggregate is not None and self._aggregate_for != wanted:
            destroy_aggregate(self._aggregate)
            self._aggregate = None
        if self._aggregate is None:
            self._aggregate = create_aggregate(out.uid, self._input_device)
            self._aggregate_for = wanted
        if self._aggregate is None or not pin_input_unit(input_node.audioUnit(), self._aggregate):
            logger.warning(f"could not select input device {self._input_device!r}; using the system default")
            return
        logger.info(f"microphone: {self._input_device!r} (with {out.name} as the output)")

    def _remove_tap(self):
        if self._tap_installed:
            self._engine.inputNode().removeTapOnBus_(0)
            self._tap_installed = False

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
        self._tap_installed = True

    def _on_configuration_change(self, notification):
        if self._loop:
            self._loop.call_soon_threadsafe(self._reconfigure)

    def _reconfigure(self):
        """A default device changed, or the unit was reconfigured. The
        engine has stopped and the hardware rate may differ. Whether voice
        processing may keep the chosen microphone may have changed too (a
        headset came or went): then the engine is built again, otherwise
        the tap is reinstalled and it restarts."""
        was_rate = self._in_rate
        self._running = False
        try:
            if self._voice_processing_allowed() != self._vp_active or (self._input_device and not self._vp_active and self._aggregate_for[0] != (default_output().uid if default_output() else "")):
                self._rebuild()
                logger.info(f"audio device changed: input {was_rate} -> {self._in_rate} Hz, engine rebuilt")
                return
            self._remove_tap()
            if self._vp_active:
                self._install_tap()
            error = self._try_start()
            if error is not None:
                logger.error(f"audio engine did not restart after device change: {error}")
                return
            logger.info(f"audio device changed: input {was_rate} -> {self._in_rate} Hz, engine restarted")
        except Exception as e:  # noqa: BLE001 - report, keep the pipeline alive
            logger.error(f"audio engine reconfiguration failed: {e}")

    def _rebuild(self):
        """Tear the engine down and build a new one with the current choices."""
        was_running = self._running or not self._built
        self.stop()
        self._engine = AVF.AVAudioEngine.alloc().init()
        self._built = False
        self._build()
        if was_running:
            # The pin makes the unit reconfigure; the first start may fail.
            error = None
            for _ in range(START_ATTEMPTS):
                error = self._try_start()
                if error is None:
                    break
                time.sleep(START_RETRY_SECS)
            if error is not None:
                raise RuntimeError(f"AVAudioEngine failed to start: {error}")

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
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._muted = False
        self._resampler = create_stream_resampler()
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        self._loop = self.get_event_loop()
        self._engine.set_input_handler(self._loop, self._on_audio)

    def receive_message(self, message: dict):
        """A message from the client (the app's page), from any thread.

        Goes upstream: ``PipelineWorker`` puts the RTVI processor in front of
        the pipeline, so upstream is the direct route to it.
        """
        if self._loop is None:
            logger.warning("mac transport: message before setup, dropped")
            return
        asyncio.run_coroutine_threadsafe(
            self.push_frame(InputTransportMessageFrame(message=message), FrameDirection.UPSTREAM), self._loop
        )

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._queue = asyncio.Queue()
        self._task = self.create_task(self._drain(), name="mic")
        await self._engine.start_async()
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
        if self._queue and not self._muted:
            self._queue.put_nowait((pcm, rate))

    def set_muted(self, muted: bool):
        """Drop the microphone at the source: nothing reaches the recognizers
        while muted. The engine keeps running so speech still plays."""
        self._muted = muted

    async def _drain(self):
        chunk_bytes = int(self.sample_rate * TAP_BUFFER_SECS) * 2
        # The microphone level, logged now and then: the first thing to look
        # at when nothing is heard.
        peak, since = 0.0, time.monotonic()
        while True:
            pcm, rate = await self._queue.get()
            samples = np.clip(np.frombuffer(pcm, dtype=np.float32), -1.0, 1.0)
            peak = max(peak, float(np.max(np.abs(samples))) if samples.size else 0.0)
            if time.monotonic() - since >= MIC_LEVEL_LOG_SECS:
                db = 20 * math.log10(peak) if peak > 0 else -120.0
                logger.debug(f"mic: peak {db:.0f} dBFS over the last {MIC_LEVEL_LOG_SECS:.0f} s ({rate} Hz in)")
                peak, since = 0.0, time.monotonic()
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

    def __init__(self, engine: _Engine, params: MacAudioTransportParams, *, send_to_client: Callable[[dict], None]):
        super().__init__(params)
        self._engine = engine
        self._play_head = 0.0
        self._send_to_client = send_to_client

    async def send_message(self, frame: OutputTransportMessageFrame | OutputTransportMessageUrgentFrame):
        """A message for the client (RTVI envelopes from the observer)."""
        self._send_to_client(frame.message)

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        self._engine.set_output_rate(self.sample_rate)

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._engine.start_async()
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
    """Local audio on macOS through one ``AVAudioEngine``, plus a message
    channel for the app's own client: the RTVI envelopes the pipeline emits
    go out through ``send_to_client``, and what the client sends comes in
    through :meth:`receive_message`. Any in-process UI (a web view, a native
    window) can be the client.

    Events:
        on_ready: The engine is running and the pipeline has started.
    """

    def __init__(
        self,
        params: Optional[MacAudioTransportParams] = None,
        *,
        send_to_client: Optional[Callable[[dict], None]] = None,
    ):
        super().__init__()
        self._params = params or MacAudioTransportParams(audio_in_enabled=True, audio_out_enabled=True)
        self._engine = _Engine(voice_processing=self._params.voice_processing, input_device=self._params.input_device)
        self._send_to_client = send_to_client
        self._input: Optional[MacAudioInputTransport] = None
        self._output: Optional[MacAudioOutputTransport] = None
        self._register_event_handler("on_ready")

    def set_client(self, send_to_client: Callable[[dict], None]):
        """Where messages for the client go. Can be set after construction,
        but before the pipeline sends anything."""
        self._send_to_client = send_to_client

    def receive_message(self, message: dict):
        """A message from the client, from any thread."""
        self.input().receive_message(message)  # type: ignore[attr-defined]

    def set_muted(self, muted: bool):
        """Mute or unmute the microphone."""
        self.input().set_muted(muted)  # type: ignore[attr-defined]

    def set_voice_processing(self, enabled: bool):
        """Turn the OS echo canceller on or off, live."""
        self._engine.set_voice_processing(enabled)

    def set_input_device(self, uid: str):
        """Use another microphone, live; "" follows the system default."""
        self._engine.set_input_device(uid)

    @staticmethod
    def input_devices() -> list[InputDevice]:
        """The microphones present right now."""
        return input_devices()

    def _deliver(self, message: dict):
        if self._send_to_client:
            self._send_to_client(message)
        else:
            logger.debug(f"mac transport: no client for message {message.get('type')}")

    def input(self) -> FrameProcessor:
        if not self._input:
            self._input = MacAudioInputTransport(self._engine, self._params, on_ready=self._ready)
        return self._input

    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = MacAudioOutputTransport(self._engine, self._params, send_to_client=self._deliver)
        return self._output

    async def _ready(self):
        await self._call_event_handler("on_ready")
