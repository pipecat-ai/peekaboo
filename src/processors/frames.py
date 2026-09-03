#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from PIL import Image
from pipecat.frames.frames import SystemFrame


@dataclass
class CaptureRequestFrame(SystemFrame):
    """Ask the frame source for one frame of a target, now."""

    target: str

    def __str__(self):
        return f"{self.name}(target: {self.target})"


@dataclass
class ScreenFrame(SystemFrame):
    """One captured frame of a target, ready for analysis.

    Produced by a frame source. The change gate fills in ``signature``,
    ``key`` and ``changed`` on its way through.
    """

    target: str
    image: Image.Image
    timestamp: int
    signature: Optional[bytes] = field(default=None, repr=False)
    key: Optional[str] = None
    """Identity of the frame for storage: same key, same picture."""
    changed: bool = True
    """Whether the frame differs from the last one analyzed for this target."""
    app: Optional[str] = None
    """For a window, its app; for the screen, the frontmost app (the focus)."""
    title: Optional[str] = None
    """The window's title, or the frontmost window's for the screen."""
    role: str = "window"
    """``"screen"`` for a display still, ``"window"`` for one window."""
    moment: Optional[int] = None
    """The recording tick this frame was taken in, shared by the screen still
    and every window still of that tick."""
    rect: Optional[tuple[int, int, int, int]] = None
    """The window's place on the screen (x, y, w, h in points), when known."""

    def __str__(self):
        return (
            f"{self.name}(target: {self.target} role: {self.role} size: {self.image.size} "
            f"key: {self.key} changed: {self.changed} app: {self.app})"
        )


@dataclass
class QuestionFrame(SystemFrame):
    """A question about the screen, with the picture and recent context to answer it from."""

    query: str
    image: Optional[bytes] = field(default=None, repr=False)
    """JPEG bytes of the screen right now, when a frame was available."""
    size: Optional[tuple[int, int]] = None
    context: list[dict] = field(default_factory=list)
    """Recent observations, oldest first, in their LLM shape."""

    def __str__(self):
        return f"{self.name}(query: {self.query} image: {self.size} context: {len(self.context)})"


@dataclass
class VisionWatchlistFrame(SystemFrame):
    """An image analysis that matched one or more watchlist items."""

    content: Mapping[str, Any]

    def __str__(self):
        return f"{self.name}(content: {self.content})"
