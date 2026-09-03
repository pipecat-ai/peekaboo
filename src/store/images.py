#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import hashlib
from pathlib import Path

from PIL import Image

THUMBNAIL_WIDTH = 320
JPEG_QUALITY = 70

# The reduced grayscale image change detection and frame identity work on.
# Wide enough that a new line of terminal output moves a few pixels, small
# enough that comparing two of them is nothing.
SIGNATURE_SIZE = (128, 80)


def signature(image: Image.Image) -> bytes:
    """A small grayscale rendering of the image, as raw bytes."""
    return image.convert("L").resize(SIGNATURE_SIZE, Image.Resampling.BOX).tobytes()


def changed_fraction(a: bytes, b: bytes, *, min_delta: int = 24) -> float:
    """Fraction of signature pixels that differ by more than ``min_delta``.

    Compression noise and a blinking cursor stay under the delta or touch a
    pixel or two; a new line of text or a dialog moves many.
    """
    if len(a) != len(b) or not a:
        return 1.0
    changed = sum(1 for x, y in zip(a, b) if abs(x - y) > min_delta)
    return changed / len(a)


def changed_box(a: bytes, b: bytes, size: tuple[int, int], *, min_delta: int = 24, pad: int = 24):
    """The box in image pixels around every signature pixel that differs
    between two signatures of the same frame size, or None if nothing does."""
    w, h = SIGNATURE_SIZE
    if len(a) != len(b) or len(a) != w * h:
        return None
    xs, ys = [], []
    for i, (x, y) in enumerate(zip(a, b)):
        if abs(x - y) > min_delta:
            xs.append(i % w)
            ys.append(i // w)
    if not xs:
        return None
    sx, sy = size[0] / w, size[1] / h
    left = max(0, int(min(xs) * sx) - pad)
    top = max(0, int(min(ys) * sy) - pad)
    right = min(size[0], int((max(xs) + 1) * sx) + pad)
    bottom = min(size[1], int((max(ys) + 1) * sy) + pad)
    return (left, top, right, bottom)


def frame_key(sig: bytes) -> str:
    """Identity of a frame for deduplication: quantized so noise doesn't count."""
    quantized = bytes(v >> 4 for v in sig)
    return hashlib.sha1(quantized).hexdigest()[:20]


def dhash(image: Image.Image, size: int = 8) -> str:
    """A 64-bit difference hash of an image, as 16 hex digits."""
    gray = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    pixels = list(gray.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    """How many bits differ between two hashes from :func:`dhash`."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def save_frame(image: Image.Image, directory: Path, stem: str) -> tuple[Path, Path]:
    """Write a frame and its thumbnail as JPEGs. Returns (frame, thumbnail)."""
    directory.mkdir(parents=True, exist_ok=True)
    frame_path = directory / f"{stem}.jpg"
    thumb_path = directory / f"{stem}.thumb.jpg"

    rgb = image.convert("RGB")
    rgb.save(frame_path, "JPEG", quality=JPEG_QUALITY, optimize=True)

    width = THUMBNAIL_WIDTH
    height = max(1, round(rgb.height * width / rgb.width))
    rgb.resize((width, height), Image.Resampling.LANCZOS).save(
        thumb_path, "JPEG", quality=JPEG_QUALITY
    )

    return frame_path, thumb_path
