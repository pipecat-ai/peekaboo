#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""M0: the native audio transport's foundation. An ``AVAudioEngine`` with a
tap on the input node and a player node on the same engine, with
voice-processing I/O on so the OS cancels what the speakers play.

The test: record the mic for a moment of quiet, then play a few seconds of
speech through the speakers while still recording, then quiet again. With
voice processing on, the mic level during playback should sit near the quiet
baseline; with it off, the speakers leak straight into the mic.

    uv run spikes/audio.py            # voice processing on
    uv run spikes/audio.py --no-vp    # off, for comparison
    uv run spikes/audio.py --out mic.wav

Run both and compare the "during playback" level. Keep the room quiet and the
volume at a normal listening level.
"""

import argparse
import array
import math
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import AVFoundation as AVF
from Foundation import NSURL

OUT_DIR = Path(__file__).parent / "out"

SPEECH = (
    "Peekaboo here. The build finished a moment ago, and the terminal says all "
    "forty two tests passed. Standup starts in five minutes; want me to open the link?"
)

WINDOW_SECS = 0.5
QUIET_SECS = 2.5


def require_microphone():
    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeAudio)
    if status == AVF.AVAuthorizationStatusAuthorized:
        return
    done = threading.Event()
    granted = [False]

    def handler(ok):
        granted[0] = bool(ok)
        done.set()

    AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVF.AVMediaTypeAudio, handler)
    done.wait(60)
    if not granted[0]:
        sys.exit("Microphone access is not granted to this process. Enable it for your terminal and relaunch it.")


def synthesize_speech(text: str) -> Path:
    """Speech to play back, from the system voice, at the engine's rate."""
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / "speech.wav"
    subprocess.run(["say", "-o", str(path), "--data-format=LEF32@48000", text], check=True)
    return path


def dbfs(rms: float) -> float:
    return 20 * math.log10(rms) if rms > 0 else -100.0


class Recorder:
    """Collects channel 0 of every tap buffer and per-window levels."""

    def __init__(self, channels: int):
        self.samples = array.array("f")
        self.channels = channels
        self._lock = threading.Lock()
        self._first = True
        self.channel_levels: Optional[list[float]] = None

    def tap(self, buffer, when):
        n = buffer.frameLength()
        data = buffer.floatChannelData()
        if data is None:
            return
        # The first buffer tells us what the channels look like.
        if self._first:
            self._first = False
            levels = []
            for c in range(min(self.channels, len(data))):
                chunk = array.array("f")
                chunk.frombytes(bytes(data[c].as_buffer(n)))
                levels.append(dbfs(math.sqrt(sum(x * x for x in chunk) / max(1, len(chunk)))))
            self.channel_levels = levels
        chunk = array.array("f")
        chunk.frombytes(bytes(data[0].as_buffer(n)))
        with self._lock:
            self.samples.extend(chunk)

    def levels(self, rate: float, window: float) -> list[float]:
        with self._lock:
            samples = self.samples[:]
        step = int(rate * window)
        out = []
        for i in range(0, len(samples) - step + 1, step):
            seg = samples[i : i + step]
            out.append(dbfs(math.sqrt(sum(x * x for x in seg) / step)))
        return out

    def save(self, path: Path, rate: float):
        with self._lock:
            samples = self.samples[:]
        pcm = array.array("h", (max(-32768, min(32767, int(x * 32767))) for x in samples))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(rate))
            w.writeframes(pcm.tobytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-vp", action="store_true", help="leave voice processing off")
    parser.add_argument("--out", type=Path, default=None, help="save what the mic heard as a WAV")
    parser.add_argument("--text", default=SPEECH)
    args = parser.parse_args()

    require_microphone()
    speech_path = synthesize_speech(args.text)

    engine = AVF.AVAudioEngine.alloc().init()
    input_node = engine.inputNode()
    output_node = engine.outputNode()

    # Speech through a player on the same engine, so the canceller sees it.
    url = NSURL.fileURLWithPath_(str(speech_path))
    audio_file, error = AVF.AVAudioFile.alloc().initForReading_error_(url, None)
    if audio_file is None:
        sys.exit(f"could not read {speech_path}: {error}")
    duration = audio_file.length() / audio_file.processingFormat().sampleRate()
    player = AVF.AVAudioPlayerNode.alloc().init()
    engine.attachNode_(player)
    engine.connect_to_format_(player, engine.mainMixerNode(), audio_file.processingFormat())

    # Order matters: enable voice processing AFTER the output graph exists and
    # BEFORE the engine starts. Enabled first, the output node ends up with a
    # 0 Hz format and the engine fails to start with -10875. Enabling it on
    # the input node enables the shared voice-processing I/O unit for output.
    if not args.no_vp:
        ok, error = input_node.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            sys.exit(f"voice processing could not be enabled: {error}")
    print(
        f"voice processing: input={input_node.isVoiceProcessingEnabled()} "
        f"output={output_node.isVoiceProcessingEnabled()}"
    )

    in_format = input_node.outputFormatForBus_(0)
    rate = in_format.sampleRate()
    channels = in_format.channelCount()
    print(f"input node: {rate:.0f} Hz, {channels} channel(s); tapping mono")

    # With voice processing on, the node reports nine identical channels. A
    # mono tap format at the same rate is accepted and is what the transport
    # will use; a nil format taps the node's own (9-channel) format instead.
    tap_format = AVF.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(rate, 1)
    recorder = Recorder(tap_format.channelCount())
    input_node.installTapOnBus_bufferSize_format_block_(0, int(rate * 0.1), tap_format, recorder.tap)

    engine.prepare()
    ok, error = engine.startAndReturnError_(None)
    if not ok:
        sys.exit(f"engine failed to start: {error}")

    print(f"\nquiet for {QUIET_SECS}s...")
    time.sleep(QUIET_SECS)
    play_start = len(recorder.samples) / rate
    print(f"playing {duration:.1f}s of speech through the speakers...")
    player.scheduleFile_atTime_completionHandler_(audio_file, None, None)
    player.play()
    time.sleep(duration)
    play_end = len(recorder.samples) / rate
    print(f"quiet for {QUIET_SECS}s...")
    time.sleep(QUIET_SECS)

    engine.stop()
    input_node.removeTapOnBus_(0)

    if recorder.channel_levels:
        print("\nfirst buffer, level per channel (dBFS):", " ".join(f"{l:6.1f}" for l in recorder.channel_levels))

    levels = recorder.levels(rate, WINDOW_SECS)
    quiet, during = [], []
    print(f"\nmic level per {WINDOW_SECS}s window (channel 0, dBFS):")
    for i, level in enumerate(levels):
        t = i * WINDOW_SECS
        playing = play_start <= t < play_end
        (during if playing else quiet).append(level)
        bar = "#" * max(0, int((level + 80) / 2))
        print(f"  {t:5.1f}s {'PLAY ' if playing else 'quiet'} {level:6.1f} {bar}")

    if quiet and during:
        q = sum(quiet) / len(quiet)
        d = sum(during) / len(during)
        print(f"\nmean quiet {q:.1f} dBFS, mean during playback {d:.1f} dBFS, leak {d - q:+.1f} dB")

    if args.out:
        recorder.save(args.out, rate)
        print(f"saved {args.out}")


if __name__ == "__main__":
    main()
