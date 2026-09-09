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
        who = "Peekaboo" if bundled() else "your terminal"
        out = []
        if not self.screen_recording:
            out.append(f"Screen Recording: enable it for {who}, then relaunch it. {SCREEN_RECORDING_PANE}")
        if not self.microphone:
            out.append(f"Microphone: enable it for {who}, then relaunch it. {MICROPHONE_PANE}")
        return out


def bundled() -> bool:
    """Running from a Peekaboo.app bundle rather than a terminal."""
    import AppKit

    path = str(AppKit.NSBundle.mainBundle().bundlePath() or "")
    return path.endswith("Peekaboo.app")


async def wait_for_screen_recording(timeout_secs: float = 600.0, every: float = 2.0) -> bool:
    """Poll until Screen Recording is granted (the user clicked Allow or
    flipped the switch), or give up."""
    waited = 0.0
    while waited < timeout_secs:
        if screen_recording_granted():
            return True
        await asyncio.sleep(every)
        waited += every
    return False


def relaunch():
    """Start a fresh copy of this bundle and let this one quit: a Screen
    Recording grant only applies to a process started after it. The copy is
    told it is a relaunch, so it never relaunches in turn."""
    import os
    import subprocess

    import AppKit

    path = str(AppKit.NSBundle.mainBundle().bundlePath())
    logger.info(f"relaunching {path} so the grant applies")
    subprocess.Popen(["open", "-n", path], env={**os.environ, "PEEKABOO_RELAUNCHED": "1"})


def restart():
    """Start a fresh copy of the app and quit this one, so choices that are
    read at launch (the models) take effect. The bundle is reopened; a
    terminal run is started again with the same arguments."""
    import os
    import subprocess
    import sys

    import AppKit
    from PyObjCTools import AppHelper

    if bundled():
        path = str(AppKit.NSBundle.mainBundle().bundlePath())
        subprocess.Popen(["open", "-n", path])
    else:
        subprocess.Popen([sys.executable, *sys.argv], start_new_session=True)
    AppHelper.callAfter(AppKit.NSApp.terminate_, None)


def is_relaunch() -> bool:
    import os

    return os.environ.get("PEEKABOO_RELAUNCHED") == "1"


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
    logger.info("asking for the microphone; waiting for the answer")
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
    logger.info(
        "asking for Screen Recording: the dialog only opens System Settings; the switch there "
        "must be turned on, and the grant takes effect after a relaunch"
    )
    return bool(Quartz.CGRequestScreenCaptureAccess())


async def request_all() -> Permissions:
    """Ask for whatever is missing and report what we ended up with."""
    screen = request_screen_recording()
    mic = await request_microphone()
    perms = Permissions(screen_recording=screen, microphone=mic)
    for line in perms.missing():
        logger.error(line)
    return perms
