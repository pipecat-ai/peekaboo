#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Run Moonshine over a recording from ``--record-mic``, segment by segment.

Cuts the file where the level drops for a while (a rough stand-in for the
VAD), transcribes each segment with the model asked for, and prints what it
heard with the segment's peak level. For comparing models and filters on
the same audio:

    uv run tools/transcribe_wav.py mic-raw.wav mic-filtered.wav --model small-streaming
    uv run tools/transcribe_wav.py mic-raw.wav --model medium-streaming
"""

import argparse
import asyncio
import math
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def segments(samples: np.ndarray, rate: int, *, floor_db: float = -35.0, gap_secs: float = 0.6, min_secs: float = 0.4):
    """(start, end) sample ranges where the signal stays above the floor."""
    hop = rate // 50  # 20 ms
    levels = [20 * math.log10(max(float(np.abs(samples[i : i + hop]).max()), 1e-6)) for i in range(0, len(samples), hop)]
    out, start, quiet = [], None, 0
    for n, level in enumerate(levels):
        if level > floor_db:
            if start is None:
                start = n
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet * hop >= gap_secs * rate:
                end = n - quiet
                if (end - start) * hop >= min_secs * rate:
                    out.append((max(0, start - 10) * hop, min(len(samples), (end + 10) * hop)))
                start, quiet = None, 0
    if start is not None and (len(levels) - start) * hop >= min_secs * rate:
        out.append((max(0, start - 10) * hop, len(samples)))
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--model", default="small-streaming")
    args = ap.parse_args()

    from loguru import logger

    logger.remove()
    from pipecat.services.moonshine.stt import MoonshineSTTService

    service = MoonshineSTTService(settings=MoonshineSTTService.Settings(model=args.model))
    transcriber = service._load()
    for path in args.files:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        print(f"== {path.name}: {len(pcm) / rate:.1f} s at {rate} Hz, model {args.model}")
        for start, end in segments(pcm, rate):
            clip = pcm[start:end].astype(np.float32) / 32768.0
            peak = 20 * math.log10(max(float(np.abs(clip).max()), 1e-6))
            result = await asyncio.to_thread(transcriber.transcribe_without_streaming, clip.tolist(), rate)
            text = getattr(result, "text", None) or (result[0].text if isinstance(result, (list, tuple)) and result else str(result))
            print(f"  {start / rate:6.1f}s  {(end - start) / rate:4.1f}s  peak {peak:4.0f} dBFS  {text!r}")


if __name__ == "__main__":
    asyncio.run(main())
