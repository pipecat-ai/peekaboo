# Peekaboo for macOS (Python) — Plan

A menu bar voice assistant that knows every window on your Mac, can look at any of
them on request, watches the ones you care about and speaks up when something
happens, and remembers what you have seen so you can search it later.

This plan targets macOS 14+ only, Python end to end, Pipecat 1.8+ with the
`pipecat.workers` API (workers, bus, jobs). Linux is out of scope for now; the
only OS-specific code is the capture layer, so the door stays open.

---

## 1. Goals

1. **See every application.** Maintain a live registry of all running apps and
   their windows, and notice when windows open, close, or change title.
2. **Look on demand.** "What does the terminal say?" answers in under two
   seconds from a fresh frame of that window, whether or not it is visible.
3. **Watch and notify.** "Tell me when the build finishes" binds a watcher to a
   window or app. The assistant speaks when the condition is met, even while
   the user works in another app or Space.
4. **Remember and search.** Keep a searchable record of what was on screen,
   with verbatim text and the screenshot behind every memory. Answer questions
   like "what was the PR I looked at this morning?" by voice, and show the
   actual frame in the app when asked.
5. **Live in the menu bar.** Pause, watchers, recent memories, search, and a
   voice trigger, all one click or one phrase away. No browser tab, no server.
6. **Speak first when it matters.** "Standup starts in two minutes, want me
   to open Zoom?" Meeting reminders, watcher hits, and warnings arrive by
   voice when you're free and as a banner when you're not.

### Non-goals for v1

- Linux or Windows.
- Acting on the screen (clicking, typing).
- Shipping a signed, notarized `.app`. v1 runs from a dev checkout with
  permissions granted to the terminal. Packaging is a later decision (see §9).
- Remote or cloud-hosted bot. Everything except the model APIs stays local.

---

## 2. macOS facts the design relies on

| Fact | Confidence | Where it matters |
|---|---|---|
| `SCShareableContent` lists every display, window, and running app, including off-screen windows when asked. It also lists every native **tab** as its own window (Ghostty: four "windows" with one frame), and nothing public separates a hidden tab from a window on another Space (`isOnScreen`, `isActive`, `kCGWindowAlpha` all agree); same-app windows sharing an exact frame are folded for presentation. Menu bar agents (Creative Cloud, Alfred, autofill helpers) keep hidden windows around; the registry keeps only windows of apps with `NSApplicationActivationPolicyRegular`, which took the list from 17 windows across 22 apps to 8 across 5. | Verified in M4 | Registry |
| A one-second poll of the window list, diffed by window ID, is cheap and gives open/close/title-change events. | High | Registry |
| `NSWorkspace` posts app launch, terminate, and activate notifications, once the AppKit run loop owns the main thread (`AppHelper.runEventLoop()`); asyncio runs on a background thread and the two meet only through `AppHelper.callAfter` and `run_coroutine_threadsafe`. ScreenCaptureKit and the audio engine work unchanged from that thread. | Verified in M4 spike | Registry, app |
| `SCStream` with a single-window filter captures that window's own buffer, so occlusion and other Spaces do not matter. | High | Watch |
| `SCContentFilter` can target an application, but that filter is scoped to what the display currently shows: an app on another Space delivers nothing, not even `suspended`. Only the desktop-independent window filter follows a window across Spaces, so watchers stream windows, and an app target resolves to the app's front content window. Two window streams plus display stills run concurrently without interfering. | Verified in M2 | Watch |
| Every delivered stream frame is `complete`, changed or not; `idle` never arrives. Minimizing or hiding the target delivers one `suspended` frame with no picture, then nothing until it is back. `complete` says a picture was delivered, not that the app drew one. | Verified in M0 | Change gate, staleness |
| `SCScreenshotManager` (macOS 14) captures a single still of a window or app without opening a stream, in 45–180 ms at 1080 wide. | Verified in M0 | Look |
| Apps that track occlusion (Chrome, Electron, Safari) stop drawing when hidden, minimized, **or on another Space**: Chrome on another Space yields `complete` frames and stills whose content area is blank. Terminals and most native apps keep drawing. | Verified in M0 (Chrome, Ghostty) | Staleness UX |
| Minimized and hidden windows go silent (`suspended`). Another Space is fine for apps that keep drawing and blank for those that don't. Same-Space occlusion not yet tested. | Verified in M0 | Staleness UX |
| A tick that stills the display and every content window (13 here) takes 630–800 ms sequentially, ~40 ms per window; 1–2 windows change per 5 s tick, so hash gating keeps analysis at today's rate; 8 of 13 stills were blank (hidden tabs, off-Space browsers) and the blank detector catches them. | Verified in the M7 spike | M7 capture |
| Using `SCContentFilter` from Python needs an `NSApplication`; without one the process asserts with `CGS_REQUIRE_INIT`. `NSApplication.sharedApplication()` is enough, no run loop required. | Verified in M0 | Everything |
| Screen Recording permission is required for capture and for reading window titles. | High | Permissions |
| Capturing without the system picker triggers Sequoia's periodic re-authorization prompt (weekly on 15.0, monthly on 15.1+). | Medium-high | Permissions |
| Screen Recording and Microphone grants attach to the responsible process. Running from Terminal or iTerm means granting the terminal app, and child processes inherit. | High | Dev workflow |
| Carbon `RegisterEventHotKey` provides a global hotkey without Accessibility or Input Monitoring permission. | Medium-high | Voice trigger (v2) |
| `AVAudioEngine` with voice-processing I/O enabled on the input node gives OS-level echo cancellation, automatic gain, and noise suppression: the bot's own speech goes from +23 dB above the quiet floor to 21 dB below it. Voice processing must be enabled after the output graph is built and before the engine starts, or the engine fails with `-10875`. The input node then reports nine identical channels; a mono tap format works. | Verified in M0 | Native audio transport |
| Pipecat's bundled local audio transport is PyAudio/PortAudio: an extra C dependency, no echo cancellation, and mic permission attributed to the Python binary. | High | Why we build our own |
| pyobjc ships bindings for AppKit, AVFoundation, ScreenCaptureKit, CoreMedia, and CoreVideo. | High | Everything |

---

## 3. Product shape

**Menu bar status item** with a state icon (idle, listening, thinking, speaking,
paused, "can't see a watched window"). Dropdown:

- Pause / Resume watching
- Ask (starts a voice turn; also reachable by wake phrase or hotkey)
- Watching ▸ one item per active watcher, with Remove
- Recent ▸ last ten observations
- Search… (opens the memories window)
- Settings… (excluded apps, retention, model tier)
- Quit

**Memories window** (a webview). The place to ask what happened in the past
and see it.

- **Ask box.** Natural-language questions go to the same `history` worker the
  voice path uses. Keyword search hits the FTS index directly.
- **Results as memories.** Each result shows time, app, window title, the
  model's description, the matched verbatim text highlighted, and the
  screenshot. Clicking opens the full frame with the observation beside it.
- **Timeline.** Scrub through a day by hour with thumbnails, step between
  frames with the arrow keys.
- **"Show me."** After a spoken answer, saying "show me" opens the window at
  that memory. The history worker returns observation IDs with its answer so
  the voice worker knows what to open.
- Watcher management and settings live here too.

**Voice.** Always-on microphone gated by a wake phrase ("Peekaboo, …") in v1,
global hotkey push-to-talk in v2. Spoken answers via TTS through the Mac's
speakers, with the OS cancelling the echo so the bot never hears itself. Watch
hits are spoken proactively, deferred until the user finishes speaking if a
turn is in progress.

### App screens

Mockups live on the design canvas:
https://claude.ai/code/artifact/79ebe2c6-a035-4f15-9e14-f4c3372777c6

| Screen | What it is for |
|---|---|
| Menu bar | State icon, pause, ask, watchers, recent, search, settings. |
| Ask | The spoken question, the answer, and the memories it drew on, each with its screenshot. |
| Memory viewer | One frame large, with what was on screen, verbatim text, the link seen, and a scrubber to step through neighbouring frames. |
| Timeline | A day as hour tracks coloured by app, with paused and quiet spans, click to scrub. |
| Watchers | Live list with state (watching, can't see it, next meeting) and a "new watcher" panel that picks from the registry of apps and windows. |
| Moments | The floating card for a meeting reminder with Join and Snooze, and the banner form used when the mic is busy. |
| Settings · Moments | What Peekaboo speaks up for, when it stays quiet, and which apps' reminders count. |
| First run | The two permissions, Screen Recording and Microphone. |

Native macOS window anatomy, the plan's indigo accent, system font with
Instrument Sans as the fallback. A low-fi dark "companion" direction sits
beside the screens as the other credible choice; native light is the default.

### Moments: when Peekaboo speaks first

A moment is anything the assistant says without being asked. Four sources:

- **Watcher hits**, from vision.
- **Meetings and schedules**, read off the screen. The screen worker always
  watches for a notification, banner, or popup about a meeting or scheduled
  event, from any app: Calendar, Zoom, Meet, Teams, Slack. A hit becomes a
  reminder with the title, the time, and the join link when one is visible.
  "The screen says Standup starts in five minutes. Want me to open the link?"
  Saying "yes" or "join" opens it. The same banner is not announced twice.
  On the Mac the notification region, top right, can be its own cheap
  high-rate target so a banner that lasts five seconds is never missed.
- **Warnings**, such as a watched window that went out of sight. Banner
  only by default.

The moment policy lives in the voice worker:

- Speak only after the user's turn ends, and never over a spoken answer.
- Stay quiet when another app holds the microphone (Zoom, Meet, FaceTime,
  detected through CoreAudio's device-in-use state), when the screen is
  being shared, and optionally when a Focus is on.
- When quiet, deliver a banner now and speak when free. Snooze is a spoken
  or clicked "later".

---

## 4. Capture modes

The registry sees everything. Pixels are captured only in three deliberate
ways, so cost stays bounded no matter how many apps are open.

| Mode | What runs | Rate | Purpose |
|---|---|---|---|
| **Registry** | Window list poll + NSWorkspace notifications | 1 Hz | Know every app and window. No pixels. |
| **Look** | One `SCScreenshotManager` still of a window or app | On demand | Answer a question about a specific window right now. Image plus question in one model call. |
| **Watch** | One `SCStream` per watched target, 1 fps, scaled to ≤1080 wide | While a watcher exists | Detect a condition. Only `complete` frames pass the change gate. |
| **Record** | Per tick: one display still (kept, not analysed) plus one `SCScreenshotManager` still per capturable window, analysed only when its hash changed (decision 10, §9). Until M7 lands: one display still, analysed. | 1 tick / 2–5 s, gated per window | Build the memory record. On by default, paused from the menu. |

**Change gate.** A frame reaches the vision model only if its stream status is
`complete` and its perceptual hash differs from the last one sent for that
target. Optional second tier later: a cheap model decides "did anything
meaningful change?" before the main model runs.

**Registry exclusions.** Our own windows, non-standard window layers (menus,
overlays), windows under a size threshold, and a bundle-ID denylist that ships
with password managers pre-populated.

---

## 5. Architecture

### Process model

- **Main thread** runs the AppKit run loop (status item, popover, memories
  window). AppKit requires this.
- **Pipecat thread** runs one asyncio loop with a `WorkerRunner` and all
  workers.
- **Capture callbacks** arrive on ScreenCaptureKit's dispatch queue and hop
  into the asyncio loop with `call_soon_threadsafe`.
- **UI updates** hop the other way onto the main queue. Menu actions push
  messages into the asyncio loop.

### Workers (all on one runner, one in-memory bus)

**`voice`** — `PipelineWorker`. macOS audio transport (below) → Moonshine
(local, always on, hears everything) → wake gate → Deepgram (connected only
while awake) → context aggregator → Anthropic LLM with tools → Cartesia over
HTTP (a request per utterance) → transport output → assistant aggregator.

Asleep, nothing is connected and nothing leaves the machine: every utterance
is transcribed on the CPU and dropped unless it starts with "Peekaboo". The
words after the phrase go through at once from the local transcript, Deepgram
connects in the background for what follows, and the gate stays awake for
fifteen seconds past the bot's last word so follow-ups need no phrase; a bare
"Peekaboo" is answered with "Yes?". With `--local-speech` Kokoro speaks and
the LLM is the only network service. Typed turns (the app's Ask box, the
evals) bypass the gate.

Tools exposed to the voice LLM:

| Tool | Backed by | Behavior |
|---|---|---|
| `list_windows(app?)` | Registry (in-process) | Returns apps and window titles. |
| `look(target, question)` | Job → `vision` | Fresh still + question, one model call, spoken answer. Timeout 15 s. |
| `watch(target, condition, repeat)` | Job → `screen` | Creates a watcher, acknowledges immediately. |
| `unwatch(id)`, `list_watchers()` | Job → `screen` | |
| `search_memory(query, since, until)` | Job → `history` | Progress updates are narrated; final answer spoken. |

Watch hits and staleness warnings arrive as urgent job updates on the
long-lived watch job. A handler on the voice worker turns them into a spoken
turn, queued behind the current user turn if one is active.

**`screen`** — `PipelineWorker`. Watches the screen and keeps the record.
Owns the frame source, the change gate, and the description model, which can
be the cheapest capable tier since it runs on every changed frame. Pipeline
shape:

```
FrameSource (frames tagged with target) → ChangeGate → VisionImageProcessor
  → user aggregator → Anthropic LLM (structured output) → assistant aggregator
  → ImageContextProcessor (parse, store, emit watch hits)
```

Jobs: `capture` starts and stops the cadence, `watch` adds a watchlist item
and stays open with hits as updates, `frame` answers with a picture of a
target as JPEG bytes, fresh or the latest seen. On macOS there is one screen
worker per watched window or app, named by target; the single shared-screen
target is the first of many.

**`vision`** — `PipelineWorker`. Answers questions with the picture in hand.
A `look` job asks the screen worker for a fresh frame, reads the last few
observations from the store, and sends question, picture, and context to the
model in one call. Runs rarely, so it can use the strongest model. Delegates
past questions to the history worker and forwards what comes back.

The frame source is injected. On the Mac it is the ScreenCaptureKit source,
which reads windows straight from the OS, so nothing about the screen
touches a transport and the voice pipeline carries audio only. When the app
runs against a browser screen share instead (a remote mode, and the Linux
test rig), the source is transport-backed: the voice pipeline gets a small
screen bridge after its transport input and the vision worker is bridged
onto the bus to receive the frames. Daily and WebRTC are never part of the
Mac app.

Description schema per frame:
`{ type: "description" | "watchlist", content, verbatim_text: [..], watchlist: [..], timestamp }`.

**`history`** — `PipelineWorker`. Anthropic LLM with extended thinking and
three tools over the store: `search(query, since, until)`,
`timeline(target, since, until)`, `get(ids)`. Handles `search_memory` jobs,
sends progress updates while it searches, returns a spoken-form answer plus
the observation IDs it drew on, so the app can show the screenshots.
Replaces today's `HistoryAgent` and its batch paging.

**`ui`** — `BaseWorker`, no pipeline. Subscribes to bus messages (watcher
created / hit / stale / removed, job progress, voice state) and marshals them
to the main thread. Carries menu actions back (pause, unwatch, ask) and opens the memories window
at a given observation when the voice worker asks ("show me").

**Moment policy** is a small arbiter inside the `voice` worker: a queue of
pending moments, the quiet rules above, and the choice between speech and a
banner. Banners go through the `ui` worker to the system notification
center. Reminders reach it from the `screen` worker over a `subscribe` job.

### macOS audio transport

A new `BaseTransport` built on `AVAudioEngine` through pyobjc, replacing the
bundled PyAudio transport. This is a first-class deliverable, not a detail.

- **Input.** A tap on the engine's input node with voice-processing I/O
  enabled, which turns on the OS echo canceller, automatic gain control, and
  noise suppression. Without this an always-on mic next to the speakers hears
  the bot's own TTS and the wake phrase and VAD misfire.
- **Output.** A player node fed from the pipeline's audio frames, on the same
  engine so the echo canceller sees the reference signal.
- **Format.** The engine runs at the hardware rate; the transport resamples to
  the pipeline's 16 kHz mono int16 for STT and back for TTS output.
- **Threading.** The tap block runs on the audio thread. It copies bytes and
  hops into the asyncio loop with `call_soon_threadsafe`, nothing else.
- **Devices.** Listens for default input and output device changes and
  reconfigures without restarting the pipeline. AirPods connecting mid-session
  must not kill the conversation.
- **Permission.** Requests microphone access through AVFoundation so the
  prompt and the grant are attributed correctly.

Same `TransportParams` surface as the other transports, so the voice worker
does not know which transport it has. Keep the Daily and WebRTC transports
selectable for demos.

### Store

SQLite at `~/Library/Application Support/Peekaboo/peekaboo.db`, WAL mode.

- `windows` — window ID, app bundle ID, app name, title, first/last seen.
- `observations` — timestamp, target, app, title, kind, content,
  verbatim_text, frame hash, screenshot path, thumbnail path.
- `observations_fts` — FTS5 over content and verbatim_text.
- `watchers` — id, target, condition, repeat, created, last hit.

**Screenshots.** Every observation keeps the frame that produced it, as a
JPEG scaled to 1280 wide at moderate quality, roughly 100–200 KB, plus a small
thumbnail for lists. Frames are deduplicated by hash. Text is kept
indefinitely; images follow a retention setting, default 7 days. At the gated
rate of Record mode that is on the order of 0.5 GB a day, so a week of history
is a few gigabytes. Retention and a "delete this range" action are user
controls, not internals.

This replaces the hourly JSON files and their read-rewrite-on-append cost.

---

## 6. Permissions

| Permission | Needed for | When |
|---|---|---|
| Screen Recording | Capture and window titles | v1 |
| Microphone | Voice | v1 |
| Notifications | Banners when a moment can't be spoken | v1 |
| Accessibility | Reading window text without pixels; moving a window to another Space | Later |

Development: grant Screen Recording and Microphone to Terminal or iTerm
once.
The app checks at startup and shows the exact System Settings pane to open if
a grant is missing.

Known cost: enumerating and capturing everything means not using the system
picker, so Sequoia's periodic re-authorization prompt applies. Accepted for v1.
The picker can be offered later as the gesture for creating a watcher.

---

## 7. Milestones

| # | Milestone | Deliverable | Demo | Exit criteria |
|---|---|---|---|---|
| M0 | **Spike** | `spikes/` scripts only | Print the window list, screenshot one window by title, stream one window and log frame statuses. Record from the mic tap while playing audio and confirm the echo is gone. | Occluded and other-Space capture confirmed. `suspended` semantics confirmed. Stream callback → asyncio path works. Chrome-hidden staleness reproduced. AVAudioEngine tap works from pyobjc and voice-processing I/O cancels the bot's own output. Decision on Record mode source. |
| M1 | **Core, no UI** | macOS audio transport, runner with `voice` and `vision`, registry, `look`, SQLite store, CLI launcher | "What does the terminal say?" spoken and answered through the Mac's own mic and speakers. | Under two seconds from question end to first spoken word on a warm path. Bot output does not trigger its own VAD. Default device change mid-session survives. |
| M2 | **Watchers** | `watch` / `unwatch`, per-target streams, change gate, hit and stale notifications | "Tell me when the build finishes", switch Space, hear it. | Hit spoken within ~2 s of the change. Stale warning when the target stops drawing. Watch survives the window being covered. |
| M3 | **Memory** | Record mode, screenshots and thumbnails on disk, verbatim text extraction, FTS, `history` worker with progress narration and observation IDs | "What was the PR I looked at this morning?" | Correct answer from a morning of recording. Progress spoken while searching. Cost per hour of recording and disk per day measured and acceptable. |
| M4 | **Menu bar and memories** | Status item, state icon, dropdown, memories window with ask box, results, screenshot viewer and timeline, `ui` worker, wake phrase | Ask by voice, say "show me", the window opens on the screenshot. Then type a question in the app and get the same answer with frames. | Pause stops all streams. Watchers and recent items reflect the bus in real time. Every result opens its full-size frame. |
| M5 | **Moments** | Built-in notification watch in the `screen` worker, moment policy in the voice worker, quiet rules, banners through the `ui` worker, join flow | A calendar banner appears. Peekaboo says it, you say "join", Zoom opens. Repeat while Zoom holds the mic: a banner instead. | Reminder spoken when the banner appears. Never speaks while another app uses the mic. The same banner is announced once. Snooze works by voice. |
| M6 | **Polish** | Launch at login, exclusions, retention, hotkey, permission onboarding | | Runs for a full workday without intervention. |
| M7 | **Windows as memory** | Moments (screen still + per-window frames with rectangles), change-gated per-window analysis, blank detection for off-Space browsers, capture on visit, focus marker; cards and search on window frames, Viewer as the screen map with clickable outlines, Timeline scrubbing screen stills | "What was in Slack while I was in the terminal at three?" answered from a covered window; click the Slack outline on the screen and read it | Analysis cost within 1.5× of today's for a normal hour; every readable window of a moment stored at full resolution. |

Rough sizing: M0 two days, M1–M5 three to four days each, M6 ongoing.
About four weeks to a complete demo.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| ScreenCaptureKit stream delegate from pyobjc is under-documented and fiddly. | M0 proves it first. Fallback for Watch is 1 fps `SCScreenshotManager` polling with hashing, which loses the free `idle`/`suspended` signals but keeps the product intact. |
| AVAudioEngine tap and player from pyobjc: audio-thread callbacks, format negotiation, and voice-processing constraints on sample rate and channel count. | M0 proves the tap, the echo cancellation, and the resample path before anything depends on it. Fallback is the PyAudio transport with a software echo canceller, which is worse but unblocks the pipeline work. |
| AppKit main thread vs asyncio thread bugs. | One `ui` worker is the only crossing point. Everything else stays on the asyncio loop. |
| Vision cost while recording. | Change gate is mandatory. Measure in M3. Add the cheap first-tier model if needed. Record can be paused or scoped to a frontmost window. |
| Sequoia re-authorization prompt annoys users. | Accept for v1. Offer the system picker for watchers later. |
| Stale frames from occlusion-aware apps. | Staleness detection plus a clear menu bar state and spoken warning. Suggest moving the window to another Space. |
| Python packaging for distribution. | Deferred. If it ships, the capture and UI layers move to a Swift menu bar app with Python as a bundled sidecar (§9). |

---

## 9. Decisions

Defaults taken in this plan. Change any of them before M1.

1. **Record mode on by default**, pausable from the menu. The memory story
   needs a record to exist.
2. **Wake phrase in v1**, hotkey in v2. The wake-phrase turn strategy already
   exists in Pipecat; the hotkey needs a Carbon call via ctypes.
3. **Screenshots stored, not just thumbnails**, 7-day image retention, text
   kept. Seeing the actual frame is the point of asking about the past.
4. **A new native macOS audio transport is the default.** Built on
   AVAudioEngine with voice-processing I/O, not on PyAudio, so echo
   cancellation, gain, noise suppression, device changes, and permission
   attribution come from the OS. Keep Daily and WebRTC as `--transport`
   options for demos and for watching another machine.
5. **Minimum macOS 14** (Sonoma), for `SCScreenshotManager`.
6. **Watch targets are apps or windows.** Window is precise, app is
   forgiving. Both are offered; the LLM picks based on the request.
7. **Swift shell later, not now.** Python-only until the pipeline and watcher
   behavior are right. The Swift shell is a packaging decision, not a
   product one.
8. **Meetings come from what's on screen, not from a calendar API.** That
   is the product: a screen assistant reacts to what it sees, and it works
   for every app that puts a reminder on screen without any integration.
   The cost is that a banner is short and rarely carries a full link, so the
   join link is read when visible and the Mac watches the notification
   region at a higher rate.
9. **Quiet by default when another app has the mic.** A voice assistant
   that talks into your Zoom call is worse than one that stays silent.
10. **A memory is a moment: the screen for context, every capturable window
    for content.** Supersedes the M0 decision to record the display alone
    (kept below for the record). A display still is a composite: covered
    windows are invisible, small ones unreadable, other Spaces absent, and
    its frontmost-app tag was read as the subject of the frame when it is
    only where attention was. So each tick captures the screen still, kept
    without analysis as the map of the moment (arrangement, recognisable
    while scrubbing, cheap, shorter retention), plus a still of each
    capturable window with its rectangle; a window is analysed only when its
    hash changed, so the analysis rate stays near today's while the record
    gains full-resolution text per window, covered windows included. The
    frontmost app stays as a **focus** marker, named as such in the UI.
    Windows on another Space or minimized: capture succeeds, but
    occlusion-aware apps (browsers, Electron) deliver blank content; the
    blank detector marks them and they are refreshed when next visible
    ("capture on visit"). Cards and search show window frames; the Viewer
    shows the screen with the windows outlined and clickable; the Timeline
    scrubs the screen stills. *Original M0 decision:* record the display,
    not the frontmost window, because it shows what the user sees, survives
    focus changes, and keeps side-by-side layouts; tag each observation with
    the frontmost app and title; 1280 wide.
11. **Staleness is detected three ways, not one.** A `suspended` frame, no
    frame for a few seconds, or a `complete` frame whose content area is
    blank. Frame status alone is not a freshness signal.

12. **Peekaboo is a showcase of Pipecat core.** Anything the app needs that
    Pipecat offers is used from Pipecat, even when a local shortcut is
    quicker; anything the app needs that Pipecat lacks is added to Pipecat,
    not built app-side. The window talks RTVI (the page is a
    `@pipecat-ai/client-js` client over the web view's script bridge, the
    Mac transport carries the envelopes, the JSON-RPC bridge is gone) and is
    driven by voice through a Pipecat `UIWorker` subclass (`workers/ui.py`). Candidates to move upstream from
    what exists today: the on-demand connection for STT services (connect on
    wake, disconnect after quiet, buffer during the handshake), the wake
    gate (phonetic wake match on a local recognizer's transcripts, waking
    the cloud one), transcripts that say which service produced them, the
    macOS audio transport (AVAudioEngine with the OS echo canceller), and the
    recorded-greeting playback (a cached-TTS processor).

---

## 10. Proposed layout

```
src/
  app.py                  # entry point: AppKit on main thread, Pipecat thread
  macos/
    registry.py           # window list poll + NSWorkspace → events
    capture.py            # SCStream / SCScreenshotManager wrappers → frames
    audio.py              # AVAudioEngine transport, voice-processing I/O
    notifications.py      # system banners
    permissions.py        # TCC checks and guidance
    menubar.py            # status item, dropdown, state icon
    hotkey.py             # v2
  workers/
    voice.py              # includes the moment policy
    screen.py             # capture, describe, watch, on-screen reminders
    vision.py
    history.py
    ui.py
  processors/
    window_source.py      # emits image frames tagged with target
    change_gate.py        # status + perceptual hash
    vision_prompt.py      # builds the model request
    observation_sink.py   # parse, store, emit hits
  store/
    models.py
    sqlite_store.py       # + FTS5
```

What carries over from today's code: the vision system prompts and structured
output schema, the watchlist timeout logic, the voice tool design. What goes:
`AgentRunner`, `BaseAgent`, the producer/consumer processors, `HistoryAgent`
batch paging, the hourly JSON store.

---

## 11. Progress

Linux phase, in the order agreed on 2026-09-01. Each step leaves the app
runnable.

| Step | State | Notes |
|---|---|---|
| 1. Pipecat 1.8 and the worker runner | **Done** | Dependency tracks the sibling `../pipecat` checkout (editable). One `WorkerRunner` owns the voice and vision workers; history workers are added to it on demand. Producers and consumers kept for now. VAD and turn detection moved to the user aggregator; services use `Settings`; the greeting is a `developer` message. |
| 2. Bus jobs replace producers and consumers | **Done** | Three worker classes under `src/workers/`. Voice asks vision with `look` and `watch` jobs and speaks whatever comes back as updates or responses. Vision asks the long-lived history worker with `search` jobs and forwards narration and the answer. Screen frames cross the bus through a small tee after the transport input; the vision worker is bridged to accept them and its upstream frame requests come back the same way. A look waits for the frame it requested. |
| 3. SQLite store with FTS and screenshots | **Done** | `src/store/`: SQLite in WAL mode at `db/peekaboo.db` with an FTS5 index over descriptions and verbatim on-screen text, frames as JPEGs under `db/frames/YYYY/MM/DD/` deduplicated by a difference hash, thumbnails alongside, 7-day image retention pruned at startup. All store work runs on one thread so the event loop never blocks. The image model now extracts verbatim text. The history worker searches with `search_history`, `timeline`, and `available_history` and returns the ids of what it looked at. `uv run src/store/migrate.py db` imports the old hourly JSON files. |
| 4. Window source abstraction and change gate | **Done** | `src/sources/`: a frame source is the head of the vision pipeline and owns its targets; it asks for a frame of each on a one-second cadence while capturing and on demand for a look. The transport source is the only implementation: the shared screen as one target, requests upstream over the bus, frames back the same way. A change gate marks each frame with a storage key and whether the screen moved, measured on a 128x80 grayscale signature; the image branch only describes changed frames, one at a time. A look sends the fresh picture with the question in one call and only falls back to the recent descriptions when no frame arrives. Interruptions from a new question stay inside the query branch. A new watch lets the next frame through the gate. |
| 4b. Split vision into screen and vision workers | **Done** | `screen` owns the source, gate, description model, store writes, and watchers, plus a `frame` job that hands over a picture as JPEG bytes. `vision` is a linear pipeline that answers a `look` with a fresh frame from `screen` and recent observations from the store. No shared pipeline, so no cross-talk, and each side can run its own model tier. |
| 5. Moment policy and on-screen reminders | **Done** | `src/moments.py`: everything the assistant says unprompted is a moment (answer, meeting, watch hit, warning) and a policy delivers them by priority, only when nobody is talking (a conversation-state processor after the LLM tracks user, bot, and model activity), and holds unsolicited ones while a quiet rule holds, bannering instead and speaking once it lifts. Quiet rules are injected; none on Linux yet. Reminders come from the screen: the `screen` worker always watches for meeting and schedule notifications from any app, deduplicates them, and sends them to subscribers as moments with the join link read off the banner. The voice worker phrases them through the LLM and has `join_meeting` and `snooze_reminder` tools; under the eval transport links are logged, not opened. A calendar API was tried and removed: it is not what the product is about. Mic-in-use and screen-share quiet rules and real banners are Mac work. |
| 6. Evals and tests | **Done** | `uv run evals/run.py --start-bot evals/*.yaml` starts a fresh bot per scenario, runs it, prints what the bot spoke, and flags errors in the bot log. Three text-mode scenarios: tool routing, a screen question with a fixture screenshot plus a past-tense question, and a meeting notification appearing on screen. Unit tests cover the store, change detection, link extraction, and the moment policy: `uv run --with pytest pytest tests/`. See `evals/README.md`. |

macOS phase, started 2026-09-02 on macOS 26.6 with pyobjc 12.2. Python is
pinned to 3.12 (`.python-version`) because `llvmlite`, pulled in by
pipecat's `numba` dependency, has no 3.14 wheels. The pyobjc frameworks are
dependencies marked `sys_platform == 'darwin'` so a Linux checkout still
resolves.

| Step | State | Notes |
|---|---|---|
| M0. Spike | **Done** | Four scripts under `spikes/` (not `macos/` as first written, to keep the name free for the real `src/macos/` package), findings in `spikes/README.md`. Every exit criterion met except same-Space occlusion, which needs a hand on the mouse: window list with off-Space windows and titles, 1 Hz diff at 30–50 ms a poll; stills of off-Space windows in 45–180 ms; an `SCStream` from pyobjc at a steady 1 fps hopping into asyncio; `suspended` semantics (one frame, then silence); Chrome-hidden staleness reproduced, and found to apply to other Spaces too, with blank `complete` frames; `AVAudioEngine` tap and player from pyobjc with voice processing cancelling the bot's own output by ~44 dB relative to off. Two surprises that shape M1: `idle` frames never arrive, so the change gate carries the whole unchanged-frame load, and voice processing has to be enabled after the graph is built. Record mode source decided: the display (§9.10). |
| M1. Core, no UI | **In progress** | `uv run src/app.py`. `src/macos/`: `permissions` (TCC checks, prompts, and the exact Settings panes), `registry` (1 Hz window-list poll diffed by ID into open/close/retitle/shown/hidden events, exclusions for our own windows, helpers under 100 pt, and password managers; `frontmost()` from `NSWorkspace`; `NSWorkspace` launch/terminate notifications wait for the AppKit run loop in M4), `capture` (stills, streams, filters, buffer decoding, `blank_fraction` for the stale-frame check), `audio` (the `AVAudioEngine` transport: one engine for both directions, voice processing on, mono float32 tap at the hardware rate resampled to 16 kHz and sliced to 20 ms because the engine clamps tap buffers to 100 ms, int16 output at the pipeline rate paced to a 120 ms lead since the base output loop does not pace, player flushed on interruption, engine restarted on `AVAudioEngineConfigurationChangeNotification`). `src/sources/macos.py`: the display as one target via stills at 1280 wide, each frame tagged with the frontmost app and title, which now flow into `Observation.app`/`title`. Verified: registry, source, and transport each in a pipeline; a tone queued all at once plays paced and an interruption cuts it. Two changes of direction on 2026-09-02: pipecat is now the stable **1.8.1 from PyPI** (the editable sibling checkout is no longer used; the pyproject comment said to switch once the release was out), and STT and TTS are **local**, Moonshine and Kokoro, so `ANTHROPIC_API_KEY` is the only key and speech never leaves the machine. **The spoken round trip works** through the Mac's own mic and speakers, and the bot's speech never reaches the transcript: the OS canceller holds. Measured on "what does the terminal say?": Moonshine 0.4 s → voice LLM tool call 1.8 s → second LLM call for the filler 1.3 s → Kokoro 0.8 s, so the first spoken word lands at **~4.6 s**, against a 2 s target; the vision answer takes another 4.4 s and Kokoro needs 1–2 s per sentence on the CPU. Fixes so far: answers are spoken sentence by sentence (a whole paragraph took Kokoro longer than pipecat's 3 s silent-context timeout and was dropped), the Kokoro idle timeout is 15 s, and answers skip the moment policy's settle wait. Then the model tiers from §5 were set (voice router `claude-haiku-4-5`, vision `claude-opus-5` with adaptive thinking, screen describer `claude-haiku-4-5`) and the second LLM call replaced by a canned "One moment." with `run_llm=False`: **first spoken word 1.7–2.2 s after the user stops talking** (Moonshine 0.35, Haiku 1.0–1.3, Kokoro 0.4), meeting the exit criterion; the look answer is audible at 7–10 s (Opus 3.5–4.6 s to first token, then it writes several sentences, then Kokoro), so the vision prompt now asks for one or two sentences. Kokoro is the remaining tail at ~1 s per sentence on the CPU; CoreML gives no speedup and the int8 model is 2.5× slower, so a faster engine is the only lever there. Speech services then became a switch: Deepgram and Cartesia by default while the rest of the pipeline is tuned, Moonshine and Kokoro behind `--local-speech` for the comparison later. Pending: a default-device change mid-session. |
| M2. Watchers | **In progress** | The voice worker has the plan's tools: `look(question, target)`, `watch(condition, target)`, `unwatch(id | target)`, `list_watchers()`, `list_windows(app)`; the target is whatever the user said, resolved by the source through the registry (exact names and titles first, then aliases such as "the terminal" or "the browser" mapped to bundle ids, with "the … window" stripped). A watched window gets its own `SCStream` at 1 fps; the display stays on stills. Watch items carry stable ids and are bound to a target, so a window's frames are only checked against its own watchers and `unwatch` cannot shift the numbering. Staleness three ways (§9.11): a `suspended` frame, four seconds of silence, or ten seconds of blank frames, each a spoken warning on the watch job, with "I can see it again" on recovery and teardown when the window closes. Verified: hide (⌘H) is reported stale within 1 s and fresh 0.2 s after unhide; a look at "the terminal" takes a still of the Ghostty window; the `watch.yaml` eval goes watch → no hit on a running build → hit spoken on FINISHED → unwatch. Two findings on the way: the app-level `SCContentFilter` is display-scoped (nothing, not even `suspended`, for an app on another Space), so watchers are always window filters; and Haiku under structured output once looped to 14 KB and was cut mid-string, dropping the frame, so the schema now requires the id list, output is capped at 1024 tokens, and the request uses the GA `output_config.format`. Pending: a hit and a stale warning heard by voice in a live session, and same-Space occlusion. |
| M4. Spike | **Done** | `spikes/menubar.py --auto`: AppKit on the main thread, asyncio on a background thread with the real capture, registry, and audio engine on it; a status item whose menu actions cross into asyncio and whose title updates from it; `NSWorkspace` activation, launch, and terminate notifications arriving; a `WKWebView` window with JavaScript → Python (`messageHandlers`) and Python → JavaScript (`evaluateJavaScript`) both working. Findings in `spikes/README.md`. The build follows the plan's §10 layout: `app.py` restructured around the run loop, `macos/menubar.py`, the `ui` worker as the single thread crossing, then the memories window. |
| M4. Menu bar | **In progress** | `uv run src/app.py` is a menu bar app: AppKit on the main thread, the workers on an asyncio thread. `macos/menubar.py` puts the Pipecat cat in the menu bar as a template image (cut from the logo by ink luminance and fattened, `tools/menubar_icon.py`) with a dropdown: Pause/Resume watching, Watching ▸ (one removable item per watcher), Recent ▸ (last ten observations), Quit. `workers/ui.py` is the only crossing: it refreshes the menus from the screen worker and the store every two seconds, carries pause (capture stop/start) and remove (unwatch) back as jobs, and shows the voice worker's state (idle, listening, thinking, speaking) as the item's tooltip. Every exit, menu Quit, Ctrl-C, or logout, goes through `NSApp.terminate`: the app delegate answers `NSTerminateLater`, cancels the workers, and replies when they are down (0.7 s). `AppHelper.runEventLoop(installInterrupt=True)` did not stop the loop and `stopEventLoop()` terminates the process outright, hence the delegate. **Memories window** (`macos/memories.py`, `macos/assets/memories.html`): a `WKWebView` over a local page, copied under the store root so screenshots load over `file://` from the one directory the view may read. The page talks JSON to the `ui` worker: `recent`, `get`, `search` (FTS), `day` (hour coverage), `hour`, `watchers`, `unwatch` straight from the store and the screen worker, and `ask`, which hands the question to the history worker and streams its narration and answer back as events. Layout: ask box, the answer with "the memories behind the answer" (the ids the history worker returns), results as cards with thumbnail, time, app, title, and highlighted matches, a viewer with the full frame, the description, and the verbatim text, ←/→ to step through neighbours, and a day timeline of hours. Opened from the menu (Search Memories…, any Recent item) or by voice: `show_me` opens it on the observations behind the last answer. Two dev hooks on `app.py`, `--snapshot-memories` and `--memories-eval`, render and drive the page for verification without a mouse; `SCScreenshotManager` refuses to capture our own window (`-3811`), so `WKWebView.takeSnapshot` is how the page is checked. Verified: recent, timeline, keyword search, and a full ask round trip against the real store. **Rebuilt to the design canvas** (the first version was a flat search page; the mockups are an app shell): a sidebar with Ask, Timeline, Watchers, Settings and the recording status and Pause at the bottom, the traffic lights over the sidebar through a transparent title bar; Ask with the question bar, the answer with "Spoken · Drew on N memories · Open the first one", and the three-up grid of memories it drew on; the viewer with the dark stage, the Window / What was on screen / Verbatim text / Link seen panel, Open link, "Ask about this frame…", and the filmstrip with ← → and space to play; the timeline as one track per hour on a true time axis, where a block is one or more whole **minutes coloured by the app seen most in them** (mixed minutes get a diagonal overlay), so a block is never narrower than a minute however often apps alternate and any number of apps fits; quiet is hatched; the legend shows the day's top apps with counts. Moving along a track **scrubs**: the preview follows the frame nearest the mouse, click pins it, and the block's memories lay out as a strip below with app icon, name, and time on each, ← → step, Enter opens the viewer with the block as its filmstrip. (Per-app lanes were tried and dropped: they spend height on a secondary cue and do not scale.) the watchers list, built-in reminders first, each row with a checkbox to pause or resume it (the built-in included, via an `enable_watcher` job that adds or removes the watch item without forgetting the watcher), a status pill, and a red trash to remove; the list is pushed to the page whenever it changes, so watchers made by voice appear too; the new-watcher panel (app and window picker from the registry, condition, Start watching) creates through the voice worker so hits are spoken, and waits for the watcher to exist before answering. **Watchers persist**: the screen worker writes each one's intent (what was asked for, the condition, enabled) to `watchers.json` under the store on every change and recreates them when the voice worker subscribes at startup, re-resolving the target against the windows of the moment; restored watchers deliver hits on the subscribe job as `{"hit": ...}` updates, which the voice worker speaks as watch moments. A JSON file rather than the plan's table: a handful of intent records, read once and written on change; a table earns its place when hits are logged with times for the timeline; Settings with **Appearance: System, Light, Dark** (`settings.json` in the store root; the `NSWindow` appearance follows). Real app icons come from `NSRunningApplication`. Window lists show regular apps only and fold native tabs into one entry with a count. While the memories window is open the app switches from accessory to regular activation policy, so it is in ⌘-Tab and the Dock (with the logo's black cat on a white tile as its icon, `assets/appicon.png`) and its own window is in the registry like any other; it drops back to accessory when the window closes. A minimal main menu (Peekaboo, Edit with cut/copy/paste for the page's fields, Window) shows next to the Apple menu while the window is open, and a native drag strip over the sidebar's top makes the window movable despite the web view owning the mouse. ⌘-Tab labels the process "Python": that name comes from LaunchServices and needs a bundle. The menu's first item is **Open Peekaboo** (⌘O). The greeting is a fixed "Welcome to Peekaboo." with no LLM call, spoken through TTS the first time a voice says it and kept as `greetings/<service>-<voice>.wav` under the store, then played from the file. Settings gained **Start recording when Peekaboo launches** (checkbox, default on); with it off the screen worker idles until the menu, the window's button, or "start recording" aloud (a `set_recording` tool) turns it on. **Wake phrase done, the local way**: pipecat's wake tools work on transcripts and so assume a recognizer streaming to the cloud all day; instead Moonshine runs locally as the always-on recognizer and doubles as the wake detector (`processors/wake.py`), Deepgram is an on-demand subclass that connects on wake and disconnects after the quiet window, and Cartesia moved to its HTTP service so no socket is held while idle. Live, the wake word proved to be a recognition problem, not a plumbing one: Moonshine (small-streaming, the default) hears "Peekaboo, what was I doing" fine when it is one utterance, and mangles the word on its own ("P. K.", "Hey, Pico,", "Peak of.", once "Yeah"); a natural pause after the word makes the VAD cut it into its own short clip. So the match is phonetic (a sound key with edit distance one, plus the fragments actually observed), a bare wake waits two seconds for the question before saying "Yes?", Deepgram buffers audio while its socket connects, and a one-word remainder ("go.") counts as noise. Verified end to end once: wake → Deepgram connect → follow-up without the phrase → answer → disconnect after 15 s quiet. "Yeah" cannot be rescued; a dedicated wake-word model (openWakeWord, trained on synthesized "Peekaboo") is the next step if this stays flaky. **Searches**: every question, typed or spoken, is kept with its answer and frame ids (`asks` table); a Searches screen lists them by day and clicking one restores the answer on Ask; spoken answers show on Ask as they are spoken; Clear/Escape returns to Recent; a filter box narrows the list; by voice, [past_searches] finds earlier questions ("yesterday I remember asking about a PR") and the window jumps to the match. **RTVI**: the page is now a Pipecat client. `MacAudioTransport` carries messages both ways (RTVI envelopes out through the web view, the page's messages in, pushed upstream to the RTVI processor `PipelineWorker` prepends); the page runs `@pipecat-ai/client-js` 1.13 (vendored under `assets/vendor`, ES modules from `file://` need `allowFileAccessFromFileURLs`) with a `BridgeTransport` over `WKScriptMessageHandler`; data calls are `client-message` requests answered by the shell worker through the voice worker's RTVI processor (`send_server_response`), and everything the app pushes is a `ui-command` on the bus (`BusUICommandMessage` from the shell worker, a `BaseUIWorker`), translated by the voice worker and dispatched on the page by command name. Verified: client-ready/bot-ready handshake, `recent` round trip, `pause` request plus the pushed `status` command. The `ui` worker is now `shell` (`workers/shell.py`). **Voice-driven window**: `workers/ui.py` is a Pipecat `UIWorker` (Haiku) with one `reply` tool (answer spoken verbatim through the voice pipeline's TTS via `respond_to_job(tts_speak=True)`, plus optional click / navigate / highlight / scroll_to). The page streams accessibility snapshots (`startUISnapshotStream`, 400 ms debounce) that `PipelineWorker` republishes on the bus; the page is curated for it with ARIA: screens are labelled regions, memory cards are buttons named "<app> at <time>: <what was on screen>" in a `[cols=3]` grid with their inner markup excluded, the viewer's stage is an image with a label and its verbatim text excluded, the timeline's minute cells excluded, filmstrip tiles and strip frames are selectable buttons, watcher and search rows are labelled. Measured: Ask with 8 cards ≈ 380 tokens, the Viewer ≈ 300. Built-in commands (`click`, `scroll_to`, `highlight`, `focus`, `set_input_value`, `select_text`) and `navigate` are executed on the page with `findElementByRef`. The voice worker's [window] tool hands the user's words to the `respond` job and stays silent. Verified with the `--window-request` dev flag: "open the first one" → `reply(click=e12)` → the Viewer opens on that memory, "Opening the first one." spoken 1.3 s after the request. **Timeline navigator**: the day label in the header opens a month calendar as a dropdown (days shaded by how many memories they hold, `month` call, anchored under the picker); ← → move a day, ↑ ↓ a week, ⇞ ⇟ a month, Home is today; clicking an hour label zooms into that hour (one tall track with 5-minute ticks); dragging on a track selects a span, shift-click extends it, and the strip below shows what is in it with a "Selected 10:05–10:22: 12 memories" line; Escape steps back out (selection, then zoom, then the calendar). The app legend is gone from the header; hovering a block shows a popup with the app, span, count and the other apps in it. All of it works by voice through the window agent: calendar days, hour labels, blocks (when zoomed) and strip memories are labelled buttons in the snapshot, a second click on the selected strip memory opens it, and the `reply` tool's `timeline_day` / `timeline_hour` / `timeline_from` / `timeline_to` fields do the moves that need no button ("last Tuesday", "between three and four"); the agent gets the current time with each request so relative days resolve. Verified: "show me the timeline between three and four this afternoon" → `reply(timeline_day, timeline_hour=15, from 15:00, to 16:00)` → zoomed hour, 103 memories selected, strip filled, 3.5 s to speech. The dev `--snapshot-memories` capture misses JS-rendered content on this screen even with `afterScreenUpdates`; the app's own recording of its window (`observations` with app Peekaboo) is the ground truth for checking it. **Watcher targets made honest**: a watcher watches one window or the whole screen, and picking an app used to mean its front window at that moment, with a hint promising windows opened later that nothing delivered. The picker now lists "The whole screen" first, apps as headings only, windows as the choices, and says under it what will be watched. **Watchers belong to an app now**: the picker sends the chosen window by id (titles drift) plus "App: title" as the words to remember; the watcher records the app and title (shown in the list as the app), and when the window goes away it does not die: it turns into a *waiting* watcher (amber "waiting for Slack" pill, "Slack closed; I'll pick the Slack window up again when it's back") and the screen worker, subscribed to the window registry, re-attaches it to the app's next window (new stream, watch item, gate reset, "Slack is back"). Picking an app that is not open makes a waiting watcher outright. After a relaunch the saved "App: title" resolves by title first, then by the app's front window, since window ids do not survive. Verified: picked Slack → `app: Slack`, streaming the Slack window; relaunch → restored on Slack by title. **Echo cancellation is a setting**: recording a Peekaboo session with Screen Studio gave a muffled or silent voice track, because voice-processing I/O is system-wide: while any process has it on, other apps capturing the same microphone get the ducked, processed path. The transport gained `set_voice_processing(enabled)` (stop the engine, flip the input node's mode, reinstall the tap, restart: the mode can only change while the engine is stopped), Settings gained an "Echo cancellation" checkbox (on by default, off for screen recording, with headphones), the shell tells the app of setting changes (`on_setting`) so the switch is live, and the saved value is honoured at launch. `--no-voice-processing` still forces it off. Verified: off and on again through the page's RPC, engine restarted at 48 kHz both times. Measured afterwards with two processes on the built-in microphone: while one has voice processing on, the other's input drops from room noise (RMS 0.0024) to nothing (0.00003), so with the echo canceller on any other app recording the microphone records silence; off, it records normally. Not the only cause of the muffled recording, though: the bundle's log showed the engine starting at 16 kHz input around those takes, which is a Bluetooth headset in the hands-free profile, and any app holding its microphone open keeps it there (Peekaboo holds it all day to hear its name). So the transport gained **input-device selection** (`MacAudioTransportParams.input_device`, a device UID; `set_input_device` rebuilds the engine live, since the input unit's device can only be set before it starts and cannot be unset) and Settings a **Microphone** dropdown, System default by default. `macos/audio_devices.py` reads the HAL through ctypes (PyObjC has no AudioToolbox and its CoreAudio stops at constants): `spikes/input_device.py` proved `AudioUnitSetProperty(kAudioOutputUnitProperty_CurrentDevice)` on the input node's unit, the tap then running at that device's rate. Listing microphones needed care: with voice processing on, the output device and two system aggregates (`CADefaultDeviceAggregate`, `VPAUAggregateAudioDevice`) grow input streams too, and AVFoundation's `AVCaptureDevice` lists them as microphones as well; the echo canceller's reference streams are typed as unknown (built-in speakers) or as the speaker they mirror (headphones, on a Bluetooth headset's output device, which is how Pixel Buds showed up twice), where a microphone's stream is typed as a capture; excluding output-type terminals is the filter. A chosen device that is not present falls back to the default with a warning. Verified: dropdown lists System default and the built-in mic only, choosing it pins the engine and restarts it, the choice is saved and honoured at launch. **The display still is never described now.** It was still going to the model every 30 s for watchers on "the whole screen" and as a fallback for the meeting reminder, describing content the windows had already been described for. The memory is the sum of the windows: answers already came from window descriptions only. So the screen still stays as the picture of the moment (no model call), the meeting reminder reads banners only, and a watcher without a window is a watch item with no target, checked against every window and banner as they are described ("every window" in the picker and in speech). A watcher that could fire twice for one event, from the window and from its banner, stays quiet for 60 s after a hit. Verified: a run recorded 10 display stills with no description and 7 window descriptions; a no-target watch came back labelled "every window". **Moonshine hears the conversation now, not just the wake word.** Pipecat's segmented STT services (VAD-cut segments transcribed off the audio path, padded with trailing silence; pipecat from the sibling checkout, 1.8.2.dev) are good enough to try in place of the streaming Deepgram connection and its on-demand plumbing. The voice worker takes `stt="moonshine"|"deepgram"` (`--stt`, Moonshine by default; Deepgram kept, not deleted, for comparison, and its key only required when chosen). With Moonshine alone the wake gate passes the local transcripts through while awake (`local_conversation`), the phrase stripped if said again; with Deepgram behind it the gate ignores them as before. Verified: the pipeline links Moonshine → WakeGate → aggregator with nothing else, and a gate test covers both modes. To judge by ear: whether Moonshine's segment-per-utterance transcripts are accurate enough for questions, and the added latency of transcribing after the utterance ends versus streaming. First live check failed with nothing heard: Moonshine had been set not to pass audio through, and in this pipecat the VAD and turn detection live in the user aggregator, which listens to the audio that reaches it; passthrough restored and it works (VAD → segment → transcript → LLM → TTS). `pyproject.toml` now takes pipecat from the sibling checkout (`[tool.uv.sources]`, editable), because `uv run` re-syncs the environment from the lock before every run and had silently undone a manual `uv pip install -e ../pipecat`: the banner said 1.8.1 while the dev tree was thought to be in use. On 1.8.2.dev304 a spoken sentence went VAD → 2.8 s segment → transcript in 0.17 s. **Microphone choice versus echo cancellation, settled by measurement.** With a Bluetooth headset as the output, macOS's voice-processing unit insists on that headset's microphone: a device set on the unit before enabling it is replaced, one set after is rejected (-10851), and an aggregate device fares no better. AVAudioEngine's input and output nodes also share one I/O unit, so a microphone-only device pinned on it leaves the output with nothing and the engine will not start (-10875). So: a chosen microphone that is the system default needs no pin; another one is reached through a private aggregate of the current output device plus that microphone (`AudioHardwareCreateAggregateDevice`, master = the output); and when voice processing would override the choice (Bluetooth output, microphone not that headset's) the echo canceller stays off for that engine with the reason logged, since with headphones on there is nothing to cancel. Pinning makes the unit reconfigure, so the tap goes in after the engine starts in that case (before, with voice processing, whose format is not writable once running), the start is retried while the unit settles, and the configuration-change handler restarts with a fresh tap, or rebuilds when the decision itself changed because a headset came or went. A `mic: peak … dBFS` line every 5 s in the log makes a dead or wrong microphone visible. Found live: the user's Peekaboo microphone was "System default" while the default had silently become the earbuds, so "Peekaboo" went through a hands-free mic. **Window agent**: `select` can switch screens as well as click, so "open the third search" from Ask goes navigate → state → click instead of stopping at the Searches screen; the voice prompt hands bare follow-ups ("just open it", "the third") to the window and forbids claiming what the window shows. **Noise, measured with the recordings** (`--record-mic DIR` writes the microphone as `mic-raw.wav`; `tools/transcribe_wav.py` runs Moonshine over it): in a pool hall at -10 dBFS peaks the raw audio still transcribed ("Peek-a-boo opened the window", "Can you please open the window?") while pipecat's RNNoise filter turned the same audio into "Speak the book" and "Can you please take a little note?", so RNNoise is not used. Moonshine's medium-streaming model heard the same clip cleaner than small-streaming ("Peekaboo, can you please open the window") and is the default now (`--stt-model`). **Models are a setting** (`src/models.py`, Settings ▸ Models): speech stays on the machine, Moonshine hears (and is the wake word; its model is a dropdown) and Kokoro speaks (the default voice now; its 54 voices are read from its voices file), and two language models are chosen as a provider plus a model each, Anthropic by default or OpenAI: the *Voice LLM* runs the conversation and, as its helpers, the window agent and the history answers; the *Vision LLM* describes frames and answers "look". The workers build their services through one factory, so a provider is one place to add; Anthropic-only features (thinking, the client read timeout) apply only there. One API key per provider, entered in Settings and kept in the login keychain through the `security` tool (an item it made it reads back without a prompt, from the terminal and the bundle alike); the app reads no `.env` at all, and only the key rows for the providers in use are shown, so with both LLMs on Anthropic there is one. The cloud speech services, a development option, still take their keys from the shell. **History and vision are Pipecat LLM workers now.** `history` is an `LLMContextWorker` (its context and aggregators come with the class) and `vision` an `LLMWorker` with a pipeline of its own, the query processor in front of the aggregators; their tools are `@tool` methods whose schemas come from signatures and docstrings, and the end of a turn is the assistant aggregator's `on_assistant_turn_stopped` (narration when a function call is in progress, else the answer), so the turn collector processor is gone. Both start active, since activation is what sets the tools. Three things surfaced on the way: the Vision LLM's adaptive thinking failed on Haiku 4.5 with a 400, and extended thinking is off everywhere now, every answer being spoken or a picture read, so speed wins; the screen worker's structured-output format had been lost in the move to the model factory (descriptions came back as prose and nothing parsed), and the factory now takes a JSON schema and asks each provider for it in its own way; and the built-in reminder, banner-only since the display stopped being described, watches the screen too where the frame source captures no banners (the eval transport). The eval runner gives each scenario a fresh store (`PEEKABOO_STORE`), because the reminders one run announced were deduplicated in the next, and skips the websocket handshake traceback the bot logs for a readiness probe. Voice prompt: act on the likeliest reading and acknowledge in a word; ask only when lost; "open the window" opens it, no question about which screen. Evals: 4/4. **The voice worker is an `LLMWorker` too**, around its transport pipeline: its eleven tools are `@tool` methods, the hand-written schemas are gone, and the tools reach the context through activation like the other workers'. Frames its tools queue are not deferred (`defer_tool_frames=False`): the "One moment." is spoken while the job it started runs. `select` on the window agent takes the Timeline moves as well, since the model reached for them there. Evals: 4/4. **Exclusions** (M6): Settings ▸ Recording lists the apps never recorded, password managers and the system's secret stores from the start (`src/exclusions.py`), plus any running app added from a dropdown. The registry leaves an excluded app's windows out, so nothing captures, describes, watches, or lists them, and the screen still's filter excludes the app too; a change applies at once (the registry reports the windows closed, the screen filter is rebuilt). What was recorded before an app was excluded stays in the store. Verified live: excluding Discord dropped its window from the registry and the picker within a tick; removing it brought it back. Model changes are saved at once and apply at the next launch: a notice with a Restart button appears (the bundle is reopened, a terminal run started again). Without a key for a chosen provider the app comes up anyway and opens the window on Settings. Cartesia and Deepgram stay behind `--tts cartesia` and `--stt deepgram`. Found on the way: the web view hands the page's messages over as Foundation collections, which JSON could not serialise once a setting became an object; they are converted to plain Python on arrival now. **Questions about what the Timeline shows** ("what was I working on in the last block around three") go to the window agent, not to a search: the voice router treats blocks, hours and selections as window talk, and the agent has a `select` tool that clicks and returns the fresh `<ui_state>` (waiting for the page's next snapshot), so it can click the block, see its memories, and answer with `reply` from their names. Verified: zoomed at 15:00, the question produced select(last block) then "You were working in Peekaboo from 3:39 to 3:40…". Pending: the state icon (a dot on the cat), pause stopping window streams too. **Follow-ups found on the way:** twice in a row Deepgram's connection timed out during pipeline setup and the voice worker's failure took the whole runner, and the app, down with it; startup should retry the speech services or come up without them. And in one eval run the history worker's Anthropic stream stalled after its first token and the bot sat silent for twelve minutes, past `SEARCH_TIMEOUT_SECS` (120 s), whose warning never appeared; the scenario had already passed, so the suite was green. A stalled stream must not wedge a search: give the history LLM a per-request read timeout and find out why `wait_for` did not fire inside a `sequential` job. |
| M7. Windows as memory | **In progress** | Step 1 landed: each recording tick (every 2 s) stills the display and every content window of another app, concurrently (10 windows ≈ 400 ms); blank stills (hidden tabs, off-Space browsers) are skipped; every frame carries the tick's `moment` and, for windows, its `rect`, stored in two new columns (schema 2, added in place to existing stores). Window targets reuse the watchers' `window:<id>` form, so a watched window's stream frames stand in for its stills. The image processor no longer drops changed frames while the model is busy: the newest per target waits, and cost is bounded by a per-target minimum interval (15 s for a window, 30 s for the screen still, which is context now). Measured with a YouTube video playing: 14 analyses in 90 s across Chrome, Discord and the terminal, against one every tick before. Step 2 landed: every changed screen still is stored as a `screen` observation (context, not analysed) with the display bounds as its `rect`; content queries (recent, search, timeline) exclude screen frames taken as part of a moment, so cards, search and the strip show window frames while pre-M7 screen descriptions stay; `moment`, `moments_around` and `day_stills` calls serve the Viewer and the scrubber. The Viewer opens a memory as its moment: the screen still on the stage with every captured window outlined (rectangles in display points placed as percentages of the display bounds), the selected one highlighted, click to read another, a Screen / Window toggle, the header naming the focus ("in Slack · 4 windows"), and a filmstrip that steps through moments. The Timeline's blocks are coloured by focus (from the stills) and the scrubber popup shows stills. Verified from the page: a window card opened its moment with 4 outlines, clicking another outline switched the panel, the toggle showed the window alone. Also: screen stills lose their images after 2 days (window frames after 7); the screen worker logs the analysis rate and its rough cost every 20 analyses; while recording is paused, watcher hits are still delivered but nothing is stored, stills included; the history worker's Anthropic client has a 60 s read timeout so a stalled stream fails and the 120 s job timeout can report, instead of hanging for minutes. The Deepgram setup timeout that used to take the app down no longer applies: Deepgram connects on wake, in a background task, not at startup. **Banners as their own window** (spiked and built the same evening): Notification Center draws banners in one display-sized window that is on screen only while a banner shows; captured alone it holds just the banner (361×105 px, readable, 130 ms). The registry tracks that window, each tick stills it, crops the lit part, and pushes it as a `banner` frame analysed within 3 s with the notification query; the screen still is no longer analysed at all unless a user watcher targets the screen. Verified live: a test notification became a meeting moment and was spoken, with the join link, and zero screen analyses ran. Capture on visit needs no extra code: blank stills are never pushed, so a window that becomes visible with new content trips the hash gate and is analysed. **Watchers on change**: "I get new messages" on Slack never fired because one frame cannot show what is new, and "no frames for 4 s" flagged Slack as hidden every few seconds because Electron apps only repaint on change. Now a watched window is stale only when the registry says it is off screen (or hidden/minimized), the spoken warning waits 15 s and "I can see it again" follows only a spoken warning; each target's last description travels with the next frame as "previous"; where the frame changed is cut out, enlarged up to 3× and sent as a second image; messaging apps get their unread conversations named; and the query names the app and window title (the model kept calling Slack "Notion"). Verified live 2026-09-03: "You have one new message in the gradient-bang channel from Marcus Gare…" spoken 25 s after restart, a second hit for client-sdk-squad. The menu bar cat now carries the state as a dot at its bottom right, drawn into the template image so it tints with the menu bar: filled while listening, a ring while thinking, a dot in a ring while speaking, plain when idle. A title change on a watched window ("1 new item" → "3 new items") takes a still at once, marked priority: it passes the gate as changed, skips the per-target interval, and the query carries the title before and after. Peekaboo itself is out of the record: its windows were already skipped as window frames; now the display filter excludes our own application (once the registry lists it; the screen filter is rebuilt until then), so the screen still never shows the Peekaboo window and scrolling it is not a change, and while Peekaboo is in front the focus marker keeps the app before it (or, at startup, the frontmost other window). A restart no longer re-describes every open window: the store hands the image processor each window's last analysed frame key and description (`last_frames`, two days back), the first frame of a window is skipped when it is that same frame, and the description carries over as "previous" for watch conditions. Two starts in a row: six windows seeded, only the changed two or three analysed. **Listening can be paused** too: "Pause listening" in the menu and a Mute button in the window drop the microphone at the transport (the engine keeps running so speech still plays) and put the wake gate to sleep, so Deepgram disconnects and nothing is heard; the status line says "muted" and the cat's tooltip "not listening". A look now also carries the latest capture of every open window as the present, so "do I have new mail" is answered from the Gmail window recorded minutes ago even when only the terminal is in view. Next: an exclusion list (bundle-ID denylist with password managers pre-filled, exclude an app from Settings); onboarding while a permission is missing; mark windows not seen for a while in the UI; a workday soak for real cost; the rest of packaging (M6) last. |
| M6. Packaging | **In progress** | `uv run tools/make_app.py` builds `dist/Peekaboo.app`, a development bundle around the checkout: AppKit takes the app's name and icon from the bundle that holds the running executable, so a copy of the framework's `Python.app` interpreter binary sits in `Contents/MacOS` with a `pyvenv.cfg` beside it and `Contents/lib` pointing at the checkout's `.venv`; the launcher execs it on `src/app.py`, logging to `~/Library/Logs/Peekaboo.log`. Info.plist carries the identifier `ai.pipecat.peekaboo`, `LSUIElement`, the icon (`.icns` built from `appicon.png` with sips and iconutil) and the usage strings; an ad-hoc signature gives it a stable identity in the privacy database. Verified: `open dist/Peekaboo.app` shows in `lsappinfo` as Peekaboo with the executable inside the bundle, and `quit app "Peekaboo"` reaches it. First launch asks for Microphone and Screen Recording as Peekaboo (the old grants belonged to the terminal); the screen grant needs a relaunch. A wrapper that ran `uv run` was tried first and kept the name Python: the process AppKit sees must be the bundle's own. **Made to work under `open` (2026-09-03)**: three findings. (1) The status item was parked at the origin because the bundle's declared executable was a shell script that exec'd into Python; AppKit places status items by the app's declared executable, so the interpreter binary itself is now `Contents/MacOS/Peekaboo` and the app starts from a `sitecustomize` module found via `LSEnvironment.PYTHONPATH`. (2) Screen Recording was asked for on every launch even with the switch on: an ad-hoc signature changes per build, and worse, the seal was invalid at runtime (a symlink to `.venv` inside the bundle, a `.pyc` written into Resources), and an app whose signature does not verify cannot be matched to its grant. The bundle is now signed with a local "Peekaboo Dev" certificate the builder creates on first use (requirement: identifier + certificate leaf), carries no links outside itself, and runs with `PYTHONDONTWRITEBYTECODE`; `codesign --verify --deep --strict` passes before and after a run, and `tccutil reset ScreenCapture ai.pipecat.peekaboo` cleared the stale entry. (3) The Screen Recording dialog only opens System Settings (no Allow), the switch must be turned on by hand, the grant applies to a fresh process, and `CGPreflightScreenCaptureAccess` does not flip inside the running process, so the app's relaunch-on-grant did not fire and a manual quit and reopen was needed; Quit during that wait now cancels it cleanly. The microphone dialog has Allow and stuck at once. Verified: after one grant the app launches under `open` with no prompt, menu bar item in place, recording, watcher restored. Next: an onboarding screen in the window while a permission is missing (the window and voice come up before the workers, which reads as "nothing works"); embed Python and the environment (no checkout needed); a DMG; launch at login. |
