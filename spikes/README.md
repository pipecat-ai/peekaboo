# M0 spikes

Throwaway scripts that prove the macOS facts the plan relies on before anything
in `src/` depends on them. Run from a terminal that has Screen Recording and
Microphone granted (System Settings → Privacy & Security); the grant attaches to
the terminal app and its children. Everything here is a candidate for
`src/macos/` in M1; nothing in `src/` imports from `spikes/`.

```
uv run spikes/windows.py                         # displays, apps, windows
uv run spikes/windows.py --poll                  # open/close/retitle events, 1 Hz
uv run spikes/shot.py --title "Terminal"         # one still of a window
uv run spikes/shot.py --app "Google Chrome"      # one still of an app's windows
uv run spikes/stream.py --title "Terminal" --seconds 20 --save
uv run spikes/stream.py --display --fps 0.5
uv run spikes/audio.py                           # echo test, voice processing on
uv run spikes/audio.py --no-vp                   # same, off, for comparison
```

Output lands in `spikes/out/` (ignored).

## Findings, 2026-09-02, macOS 26.6.2, pyobjc 12.2

### Capture

| Question | Answer |
|---|---|
| Does `SCShareableContent` list everything? | Yes: every display, app, and window, including other Spaces and off-screen windows, with titles. 40 windows here, 39 on other Spaces. A poll of the list costs 30–50 ms. |
| Do stills of windows on another Space work? | Mechanically yes, in 45–180 ms at 1080 wide, via `SCScreenshotManager`. Whether the *content* is there depends on the app (below). |
| Does an `SCStream` from pyobjc work? | Yes. An `NSObject` subclass declaring the `SCStreamOutput` and `SCStreamDelegate` protocols receives sample buffers on ScreenCaptureKit's queue (a nil `sampleHandlerQueue` is fine). Copying the pixels and `call_soon_threadsafe` into asyncio works; frames arrive at exactly the configured 1 fps. |
| What do frame statuses look like? | **Every delivered frame is `complete`, changed or not.** `idle` was never observed, so the free "unchanged" signal the plan hoped for does not exist here; the change gate's signature does that work, and it separates a ticking clock (0.3–0.8 % moved) from a static window (0.00–0.01 %) cleanly. |
| What does `suspended` mean? | Minimizing the window or hiding the app (⌘H) delivers exactly **one `suspended` frame with no picture, then nothing at all** until the window is back. Staleness detection is therefore "a `suspended` frame arrived, or no frame in N seconds", not a per-frame flag. |
| Do occlusion-aware apps stop drawing? | **Yes, and worse than expected.** A Chrome window on another Space, with a JavaScript clock ticking, produces `complete` frames whose web-contents area is **blank**, from both the stream and `SCScreenshotManager`. The same window on the current Space updates every second. Terminals (Ghostty) render fine wherever they are. So `complete` means "here is a picture", not "the app drew one"; the product needs a blank-content check on top of the status. |
| Does an app-level filter work? | Yes, `initWithDisplay:includingApplications:exceptingWindows:` streams every window of the app. Frames are display-sized. |
| Anything needed to use ScreenCaptureKit from Python? | Building an `SCContentFilter` asserts with `CGS_REQUIRE_INIT` unless the process has an `NSApplication`. `NSApplication.sharedApplication()` at import is enough; no run loop is needed for stills or streams. |
| Pixel format | Ask for `kCVPixelFormatType_32BGRA`; the `CGImage` and `CVPixelBuffer` both decode with PIL `raw`/`BGRA` and `bytesPerRow` as the stride. `CVPixelBufferGetBaseAddress` returns an `objc.varlist`; `as_buffer(bytes_per_row * height)` gives the bytes. |

### Audio

| Question | Answer |
|---|---|
| Does `AVAudioEngine` work from pyobjc? | Yes. Tap on the input node, `AVAudioPlayerNode` on the same engine, WAV out. |
| Does voice processing cancel the echo? | **Yes.** Speech played through the speakers while recording: with voice processing off the mic sits **+23.5 dB** above its quiet baseline during playback; with it on, **−21 dB below** it (the canceller plus noise suppression). About 44 dB between the two. |
| Ordering constraint | **Enable voice processing after the output graph is built and before the engine starts.** Enabled first, the output node reports a 0 Hz / 0-channel format and `startAndReturnError:` fails with `-10875`. `inputNode` → attach and connect the player → `setVoiceProcessingEnabled:` → install tap → start. Enabling it on the input node enables it on the output node too. |
| Input format with voice processing | 48 kHz, **9 identical channels**. A mono tap format at the engine rate is accepted and is what the transport should use. |
| Reading a tap buffer | `floatChannelData()` is a tuple of `objc.varlist`; `data[c].as_buffer(frameLength)` yields `frameLength * 4` bytes of float32. |
| Permission | Microphone is a TCC grant to the terminal, checked with `AVCaptureDevice.authorizationStatusForMediaType:`; `requestAccessForMediaType:completionHandler:` prompts. |

### Not tested here

- A window covered by another window on the *same* Space. The terminal is
  fullscreen on its own Space, so there was nothing to cover it with. Cover a
  window and run `stream.py --title ...` to check.
- Default device change mid-session (AirPods). M1 exit criterion.
- Electron apps other than Chrome. One Discord still on another Space had
  content, but it was not retested with a changing view.

## M4 spike: the app's process shape (`menubar.py`)

`uv run spikes/menubar.py --auto` runs AppKit on the main thread with asyncio
on a background thread carrying the real `src/macos` pieces, and drives every
action itself.

| Question | Answer |
|---|---|
| Do ScreenCaptureKit stills and streams work when the asyncio loop lives on another thread? | Yes. Completion handlers `call_soon_threadsafe` into that loop as before: a still in 132 ms, a 1 fps window stream delivering. |
| Does the audio engine work from that thread? | Yes. `_Engine` starts with voice processing on, plays, and the tap delivers while it plays. |
| Do `NSWorkspace` notifications arrive? | Yes, once `AppHelper.runEventLoop()` owns the main thread: activate, launch, and terminate. The registry can stop polling for app-level events. |
| Menu bar item and dropdown | `NSStatusItem` with `NSApplicationActivationPolicyAccessory` (no Dock icon). Menu actions are selectors on an `NSObject` bridge that hands coroutines to the loop with `run_coroutine_threadsafe`; results come back with `AppHelper.callAfter`, the only way onto the main thread. |
| `WKWebView` two-way bridge | Local HTML via `loadHTMLString:baseURL:`; JavaScript calls Python through `window.webkit.messageHandlers.<name>.postMessage` into a `WKScriptMessageHandler`; Python calls JavaScript with `evaluateJavaScript:completionHandler:`. Both directions verified. |
| pyobjc gotcha | Every method on an `NSObject` subclass is turned into a selector; Python-only helpers need `@objc.python_method` or class creation fails with `BadPrototypeError`. |
| Naming | The process shows up as "Python" in `NSWorkspace` and the app menu. A name and icon are a bundle matter, for packaging. |

## Decision: Record mode source

**A display stream**, not a frontmost-window stream. It shows what the user
actually sees, so the blank-window problem cannot reach the memory record; it
is one stream that never has to be torn down on focus changes; and it keeps
side-by-side layouts. The registry tags each observation with the frontmost
app and window title. Cost is a display-sized frame, so text is smaller than
in a window-scoped frame; capture at 1280 wide (the store's width) rather
than 1080 if legibility suffers.

## M7: a tick that captures every window (`windows_tick.py`)

Measured on 2026-09-02 with 13 content windows across Chrome, Google Drive,
Ghostty (three tabs), Slack and Discord, while working in the terminal:

| Question | Answer |
|---|---|
| How long is a tick that stills the display and every window? | **630–800 ms** for 13 windows, sequential: ~40 ms per window at 1080 wide, the display still 70–180 ms at 1280. Capturing windows concurrently should cut it further; even sequential it fits a 2–5 s tick. |
| How many windows change per tick? | **1–2 of 13** every 5 s while typing in one window. With hash gating, the analysis rate stays where it is today (one frame per tick), the record just gains the windows that did change. |
| How much would be stored? | The display still is 50–125 KB; changed windows 26–119 KB per tick. Same order as today's 9 MB/h. |
| How many come back blank? | **8 of 13**: Ghostty's hidden tabs (SCK lists each tab as a window; only the visible one paints), Chrome and Drive windows on another Space or hidden. The blank detector (`blank_fraction ≥ 0.97`) catches all of them; those windows must be skipped or marked, not analysed. |
