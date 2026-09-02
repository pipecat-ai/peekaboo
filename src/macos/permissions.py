#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""TCC permissions: what we have, how to ask, and where to send the user.

Grants attach to the responsible process. Run from a terminal, that is the
terminal app, and the terminal has to be relaunched after a grant. The
checks never prompt; the requests do.
"""

import asyncio
from dataclasses import dataclass

import AVFoundation as AVF
import Quartz
from loguru import logger

SCREEN_RECORDING_PANE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
)
MICROPHONE_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"


@dataclass(frozen=True)
class Permissions:
    screen_recording: bool
    microphone: bool

    @property
    def all_granted(self) -> bool:
        return self.screen_recording and self.microphone

    def missing(self) -> list[str]:
        """Human guidance for each grant we lack, one line per permission."""
        out = []
        if not self.screen_recording:
            out.append(
                "Screen Recording: enable it for your terminal, then relaunch the "
                f"terminal. {SCREEN_RECORDING_PANE}"
            )
        if not self.microphone:
            out.append(
                f"Microphone: enable it for your terminal, then relaunch the terminal. {MICROPHONE_PANE}"
            )
        return out


def screen_recording_granted() -> bool:
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def microphone_granted() -> bool:
    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeAudio)
    return status == AVF.AVAuthorizationStatusAuthorized


def check() -> Permissions:
    return Permissions(screen_recording=screen_recording_granted(), microphone=microphone_granted())


async def request_microphone() -> bool:
    """Prompt for the microphone if undecided. Returns the grant state."""
    if microphone_granted():
        return True
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def handler(granted):
        loop.call_soon_threadsafe(future.set_result, bool(granted))

    AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVF.AVMediaTypeAudio, handler)
    return await future


def request_screen_recording() -> bool:
    """Prompt for Screen Recording if undecided. The grant only takes effect
    after the responsible process is relaunched, so this returns the current
    state, which is usually still False right after the prompt."""
    if screen_recording_granted():
        return True
    return bool(Quartz.CGRequestScreenCaptureAccess())


async def request_all() -> Permissions:
    """Ask for whatever is missing and report what we ended up with."""
    screen = request_screen_recording()
    mic = await request_microphone()
    perms = Permissions(screen_recording=screen, microphone=mic)
    for line in perms.missing():
        logger.error(line)
    return perms
