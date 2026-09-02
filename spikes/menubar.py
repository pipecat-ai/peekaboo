#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""M4 spike: the app's process shape.

AppKit owns the main thread and runs the event loop; asyncio runs on a
background thread with the real ``src/macos`` pieces on it. This proves:

- ScreenCaptureKit stills and streams still work when their completion
  handlers hop into a loop that lives on another thread.
- The audio engine (the transport's ``_Engine``) starts and plays from that
  thread while the tap delivers.
- ``NSWorkspace`` notifications arrive now that there is a run loop, so the
  registry can stop polling for app launch and activation.
- A menu bar item with a dropdown whose actions cross into asyncio, and
  asyncio results that cross back to update the item.
- A ``WKWebView`` window loading local HTML, with JavaScript calling Python
  (``window.webkit.messageHandlers``) and Python calling JavaScript.

    uv run spikes/menubar.py          # click around; Quit from the menu
    uv run spikes/menubar.py --auto   # drives every action itself, then quits
"""

import argparse
import asyncio
import math
import sys
import threading
import time
from pathlib import Path

import AppKit
import objc
import WebKit
from Foundation import NSObject
from loguru import logger
from PyObjCTools import AppHelper

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from macos.audio import _Engine  # noqa: E402
from macos.capture import (  # noqa: E402
    FrameStream,
    display_filter,
    stream_configuration,
    take_still,
    window_filter,
)
from macos.registry import WindowRegistry  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss.SSS} [{thread.name}] {message}")

HTML = """<!doctype html><meta charset=utf-8>
<title>Peekaboo memories</title>
<style>body{font:14px -apple-system,system-ui;margin:24px;color:#222}button{font:inherit;padding:6px 12px}
#log{margin-top:16px;padding:12px;background:#f3f3f6;border-radius:8px;white-space:pre-wrap}</style>
<h2>Memories (spike)</h2>
<p>Python → JS: <b id=from-python>waiting…</b></p>
<button onclick="window.webkit.messageHandlers.peekaboo.postMessage({kind:'ask', text:'what was on my screen?'})">Ask Python</button>
<div id=log></div>
<script>
function fromPython(text){document.getElementById('from-python').textContent=text;
  document.getElementById('log').textContent+=text+'\\n';}
</script>"""


class Bridge(NSObject):
    """The one place the two threads meet. Lives on the main thread."""

    def initWithLoop_(self, loop):
        self = objc.super(Bridge, self).init()
        if self is None:
            return None
        self.loop = loop
        self.item = None
        self.window = None
        self.webview = None
        self.received = []
        return self

    # --- main thread -> asyncio

    @objc.python_method
    def run(self, coro):
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def takeStill_(self, sender):
        self.run(actions.still())

    def stream_(self, sender):
        self.run(actions.stream(3.0))

    def playTone_(self, sender):
        self.run(actions.tone())

    def openWindow_(self, sender):
        self.open_window()

    def quit_(self, sender):
        self.run(actions.shutdown())

    # --- asyncio -> main thread (always via AppHelper.callAfter)

    @objc.python_method
    def set_title(self, text):
        AppHelper.callAfter(self.item.button().setToolTip_, text)

    @objc.python_method
    def tell_page(self, text):
        def go():
            if self.webview:
                self.webview.evaluateJavaScript_completionHandler_(
                    f"fromPython({text!r})", None
                )

        AppHelper.callAfter(go)

    # --- NSWorkspace

    def appActivated_(self, notification):
        app = notification.userInfo()["NSWorkspaceApplicationKey"]
        logger.info(f"NSWorkspace: activated {app.localizedName()}")
        self.received.append(("activated", str(app.localizedName())))

    def appLaunched_(self, notification):
        app = notification.userInfo()["NSWorkspaceApplicationKey"]
        logger.info(f"NSWorkspace: launched {app.localizedName()}")

    def appTerminated_(self, notification):
        app = notification.userInfo()["NSWorkspaceApplicationKey"]
        logger.info(f"NSWorkspace: terminated {app.localizedName()}")

    # --- WKScriptMessageHandler

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = message.body()
        logger.info(f"JS -> Python: {dict(body) if hasattr(body, 'keys') else body}")
        self.received.append(("js", dict(body)))
        self.tell_page(f"Python heard: {dict(body).get('text')}")

    @objc.python_method
    def open_window(self):
        if self.window:
            self.window.makeKeyAndOrderFront_(None)
            return
        config = WebKit.WKWebViewConfiguration.alloc().init()
        config.userContentController().addScriptMessageHandler_name_(self, "peekaboo")
        self.webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            AppKit.NSMakeRect(0, 0, 640, 420), config
        )
        self.webview.loadHTMLString_baseURL_(HTML, None)
        mask = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskResizable
        )
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(200, 200, 640, 420), mask, AppKit.NSBackingStoreBuffered, False
        )
        self.window.setTitle_("Peekaboo memories (spike)")
        self.window.setContentView_(self.webview)
        self.window.setReleasedWhenClosed_(False)
        self.window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        logger.info("window opened")


class Actions:
    """What the menu does, all on the asyncio thread."""

    def __init__(self):
        self.registry = None
        self.engine = None
        self.frames = 0

    async def start(self):
        self.registry = WindowRegistry()
        await self.registry.start()
        bridge.set_title(f"👀 {len(self.registry.windows)}")
        logger.info(f"registry up: {len(self.registry.windows)} windows")

    async def still(self):
        display = self.registry.displays[0]
        filter = display_filter(display)
        t0 = time.monotonic()
        image = await take_still(filter, stream_configuration(filter, max_width=1280))
        logger.info(f"still: {image.size} in {(time.monotonic() - t0) * 1000:.0f} ms")
        bridge.set_title(f"👀 still {image.size[0]}x{image.size[1]}")
        bridge.tell_page(f"took a still {image.size}")

    async def stream(self, seconds: float):
        window = self.registry.find_window("the terminal") or self.registry.windows[0]
        filter = window_filter(self.registry.sc_window(window.id))
        loop = asyncio.get_running_loop()
        got = []

        def on_frame(frame):
            got.append((time.monotonic(), frame.status_name, frame.image.size if frame.image else None))
            if frame.image is not None:
                bridge.set_title(f"👀 {len(got)}")

        stream = FrameStream(filter, stream_configuration(filter, max_width=1080, fps=1.0), on_frame=on_frame)
        await stream.start()
        await asyncio.sleep(seconds)
        await stream.stop()
        logger.info(f"stream of {window.app}: {len(got)} frames in {seconds}s: {[g[1] for g in got]}")
        bridge.tell_page(f"streamed {window.app}: {len(got)} frames")

    async def tone(self):
        if self.engine is None:
            self.engine = _Engine(voice_processing=True)
            taps = []
            self.engine.set_input_handler(asyncio.get_running_loop(), lambda pcm, rate: taps.append(len(pcm)))
            self.engine.set_output_rate(24000)
            self.engine.start()
            self._taps = taps
        n = 24000
        pcm = b"".join(
            int(6000 * math.sin(2 * math.pi * 440 * i / 24000)).to_bytes(2, "little", signed=True) for i in range(n)
        )
        before = len(self._taps)
        self.engine.schedule(pcm)
        await asyncio.sleep(1.2)
        logger.info(f"tone played; tap delivered {len(self._taps) - before} buffers meanwhile")
        bridge.tell_page("played a tone")

    async def shutdown(self):
        if self.engine:
            self.engine.stop()
        if self.registry:
            await self.registry.stop()
        logger.info("shutting down")
        AppHelper.callAfter(AppHelper.stopEventLoop)

    async def auto(self):
        """Drive everything without a mouse, then quit."""
        await asyncio.sleep(1.0)
        await self.still()
        await self.stream(3.0)
        await self.tone()
        AppHelper.callAfter(bridge.open_window)
        await asyncio.sleep(1.5)
        # Python -> JS, then JS -> Python by clicking the button from script.
        bridge.tell_page("hello from asyncio")
        AppHelper.callAfter(
            lambda: bridge.webview.evaluateJavaScript_completionHandler_(
                "document.querySelector('button').click()", None
            )
        )
        await asyncio.sleep(1.0)
        # Activate another app and come back, for the NSWorkspace notification.
        AppHelper.callAfter(
            lambda: AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        )
        finder = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications() if a.bundleIdentifier() == "com.apple.finder"), None)
        if finder is not None:
            AppHelper.callAfter(finder.activateWithOptions_, 0)
            await asyncio.sleep(1.0)
            AppHelper.callAfter(AppKit.NSApp.activateIgnoringOtherApps_, True)
            await asyncio.sleep(1.0)
        js = [r for r in bridge.received if r[0] == "js"]
        activated = [r for r in bridge.received if r[0] == "activated"]
        logger.info(f"RESULT js->python messages: {len(js)}, NSWorkspace activations seen: {len(activated)}")
        await self.shutdown()


def build_menu():
    menu = AppKit.NSMenu.alloc().init()
    for title, action in [
        ("Take a still", "takeStill:"),
        ("Stream the terminal for 3 s", "stream:"),
        ("Play a tone", "playTone:"),
        ("Open memories window", "openWindow:"),
        (None, None),
        ("Quit", "quit:"),
    ]:
        if title is None:
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            continue
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        item.setTarget_(bridge)
        menu.addItem_(item)
    return menu


def main():
    global bridge, actions
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="drive every action, then quit")
    args = parser.parse_args()

    # asyncio on its own thread, started before AppKit takes the main thread.
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="asyncio", daemon=True).start()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # menu bar only, no Dock icon

    bridge = Bridge.alloc().initWithLoop_(loop)
    actions = Actions()

    item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    # The Pipecat cat as a template image: black plus alpha, so macOS tints
    # it for light and dark menu bars and the pressed state.
    icon = AppKit.NSImage.alloc().initWithContentsOfFile_(
        str(Path(__file__).resolve().parents[1] / "src/macos/assets/menubar@2x.png")
    )
    icon.setSize_(AppKit.NSMakeSize(24, 14))
    icon.setTemplate_(True)
    item.button().setImage_(icon)
    item.setMenu_(build_menu())
    bridge.item = item

    center = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
    for name, selector in [
        (AppKit.NSWorkspaceDidActivateApplicationNotification, "appActivated:"),
        (AppKit.NSWorkspaceDidLaunchApplicationNotification, "appLaunched:"),
        (AppKit.NSWorkspaceDidTerminateApplicationNotification, "appTerminated:"),
    ]:
        center.addObserver_selector_name_object_(bridge, selector, name, None)

    asyncio.run_coroutine_threadsafe(actions.start(), loop)
    if args.auto:
        asyncio.run_coroutine_threadsafe(actions.auto(), loop)

    logger.info("AppKit event loop running on the main thread")
    AppHelper.runEventLoop()
    loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    main()
