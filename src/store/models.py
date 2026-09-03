#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

ObservationKind = Literal["description", "watchlist"]


class Observation(BaseModel):
    """One analyzed screen frame: what was on screen, and the frame itself."""

    id: Optional[int] = None
    timestamp: int
    """Unix time in seconds."""
    target: str = "screen"
    """What was captured: the shared screen for now, a window or app later."""
    app: Optional[str] = None
    title: Optional[str] = None
    kind: ObservationKind = "description"
    content: str
    verbatim_text: list[str] = Field(default_factory=list)
    """Text copied exactly as it appeared: titles, URLs, errors, numbers."""
    frame_hash: Optional[str] = None
    screenshot_path: Optional[str] = None
    moment: Optional[int] = None
    """The recording tick this frame was taken in; frames of one moment share it."""
    rect: Optional[list[int]] = None
    """For a window frame, its place on the screen: x, y, w, h in points."""
    """Relative to the store root. Cleared when the image is pruned."""
    thumbnail_path: Optional[str] = None

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)

    def for_llm(self) -> dict:
        """The compact shape handed to a model as a tool result."""
        return {
            "id": self.id,
            "time": self.datetime.isoformat(timespec="minutes"),
            "kind": self.kind,
            "content": self.content,
            "verbatim_text": self.verbatim_text,
        }


class HourCoverage(BaseModel):
    hour: int
    count: int


class DayCoverage(BaseModel):
    date: str
    hours: list[HourCoverage]
