#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The memories window: a ``WKWebView`` over a local page, talking JSON to the
``ui`` worker.

The page calls Python with ``window.webkit.messageHandlers.peekaboo.postMessage
({id, method, params})`` and gets ``{event: "result", id, result | error}``
back; Python pushes events with ``peekaboo.receive({event, payload})``. Calls
run on the asyncio loop through ``on_call``; everything AppKit runs on the
main thread, and the public methods hop there themselves.

The page is copied under the store root so ``file://`` screenshots are within
the one directory the web view is allowed to read.
"""

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Optional

import AppKit
import objc
import WebKit
from Foundation import NSObject, NSURL
from loguru import logger
from PyObjCTools import AppHelper

PAGE = Path(__file__).parent / "assets" / "memories.html"
WINDOW_TITLE = "Peekaboo"
WINDOW_SIZE = (1180, 760)
MIN_SIZE = (820, 520)


class _Handler(NSObject):
    """WKScriptMessageHandler and window delegate."""

    def initWithWindow_(self, window):
        self = objc.super(_Handler, self).init()
        if self is None:
            return None
        self._window = window
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = message.body()
        try:
            data = dict(body)
        except (TypeError, ValueError):
            logger.warning(f"memories: bad message from page: {body!r}")
            return
        self._window._on_message(data)

    def windowWillClose_(self, notification):
        self._window._on_close()


class MemoriesWindow:
    """Create on the main thread; ``open`` and ``send`` from anywhere."""

    def __init__(
        self,
        *,
        store_root: Path,
        loop: asyncio.AbstractEventLoop,
        on_call: Callable[[str, dict], Awaitable[Any]],
    ):
        self._store_root = store_root
        self._loop = loop
        self._on_call = on_call
        self._window = None
        self._webview = None
        self._handler = _Handler.alloc().initWithWindow_(self)
        self._ready = False
        self._queued: list[dict] = []

    #
    # From any thread
    #

    def open(self, ids: Optional[list[int]] = None):
        """Show the window; with ids, at those memories."""

        def go():
            # Exceptions in a callAfter callback vanish into pyobjc's stderr;
            # keep them in our log.
            try:
                if self._window is None:
                    self._build()
                self._window.makeKeyAndOrderFront_(None)
                AppKit.NSApp.activateIgnoringOtherApps_(True)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"memories window failed to open: {e}")
                return
            if ids:
                self.send("show", {"ids": list(ids)})

        AppHelper.callAfter(go)

    def send(self, event: str, payload: Any = None):
        """Push an event to the page, once it is ready."""
        message = {"event": event, "payload": payload}

        def go():
            if not self._ready or self._webview is None:
                self._queued.append(message)
                return
            self._webview.evaluateJavaScript_completionHandler_(
                f"window.peekaboo && window.peekaboo.receive({json.dumps(message)})", None
            )

        AppHelper.callAfter(go)

    def evaluate(self, javascript: str):
        """Run JavaScript in the page, for development. Any thread."""

        def go():
            if self._webview is not None:
                self._webview.evaluateJavaScript_completionHandler_(javascript, None)

        AppHelper.callAfter(go)

    def snapshot(self, path: Path):
        """Render the page to a PNG, for development. Any thread."""

        def go():
            if self._webview is None:
                logger.warning("memories: no window to snapshot")
                return

            def done(image, error):
                if image is None:
                    logger.warning(f"memories: snapshot failed: {error}")
                    return
                tiff = image.TIFFRepresentation()
                rep = AppKit.NSBitmapImageRep.imageRepWithData_(tiff)
                png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
                png.writeToFile_atomically_(str(path), True)
                logger.info(f"memories: snapshot written to {path}")

            self._webview.takeSnapshotWithConfiguration_completionHandler_(None, done)

        AppHelper.callAfter(go)

    #
    # Main thread
    #

    def _build(self):
        page = self._store_root / "ui" / PAGE.name
        page.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PAGE, page)

        config = WebKit.WKWebViewConfiguration.alloc().init()
        config.userContentController().addScriptMessageHandler_name_(self._handler, "peekaboo")
        w, h = WINDOW_SIZE
        self._webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(AppKit.NSMakeRect(0, 0, w, h), config)
        self._webview.setValue_forKey_(False, "drawsBackground")
        self._webview.loadFileURL_allowingReadAccessToURL_(
            NSURL.fileURLWithPath_(str(page)), NSURL.fileURLWithPath_isDirectory_(str(self._store_root), True)
        )

        mask = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskResizable
            | AppKit.NSWindowStyleMaskMiniaturizable
        )
        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, w, h), mask, AppKit.NSBackingStoreBuffered, False
        )
        self._window.setTitle_(WINDOW_TITLE)
        self._window.setMinSize_(AppKit.NSMakeSize(*MIN_SIZE))
        self._window.setContentView_(self._webview)
        self._window.setReleasedWhenClosed_(False)
        self._window.setDelegate_(self._handler)
        self._window.center()
        logger.debug("memories window built")

    def _on_message(self, data: dict):
        method = str(data.get("method", ""))
        if method == "ready":
            self._ready = True
            queued, self._queued = self._queued, []
            for message in queued:
                self.send(message["event"], message["payload"])
            return
        call_id = data.get("id")
        params = dict(data.get("params") or {})
        future = asyncio.run_coroutine_threadsafe(self._on_call(method, params), self._loop)

        def done(f):
            try:
                self.send("result", {"id": call_id, "result": f.result()})
            except Exception as e:  # noqa: BLE001 - reported to the page
                logger.warning(f"memories: {method} failed: {e}")
                self.send("result", {"id": call_id, "error": str(e)})

        future.add_done_callback(done)

    def _on_close(self):
        # The page is torn down with the window; build a fresh one next time.
        self._ready = False
        self._queued = []
        self._window = None
        self._webview = None
