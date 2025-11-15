#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

from dataclasses import dataclass
from typing import Any, Mapping, Optional
from pipecat.frames.frames import SystemFrame


@dataclass
class VoiceAgentStartedFrame(SystemFrame):
    pass

@dataclass
class VisionQueryFrame(SystemFrame):
    query: str
    watchlist: bool

    def __str__(self):
        return f"{self.name}(query: {self.query} watchlist: {self.watchlist})"


@dataclass
class VisionRequestFrame(SystemFrame):
    text: Optional[str] = None

    def __str__(self):
        return f"{self.name}(text: {self.text})"


@dataclass
class VisionResponseFrame(SystemFrame):
    response: str

    def __str__(self):
        return f"{self.name}(response: {self.response})"

@dataclass
class VisionWatchlistFrame(SystemFrame):
    content: Mapping[str, Any]

    def __str__(self):
        return f"{self.name}(content: {self.content})"
