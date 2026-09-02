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
| `SCShareableContent` lists every display, window, and running app, including off-screen windows when asked. | High | Registry |
| A one-second poll of the window list, diffed by window ID, is cheap and gives open/close/title-change events. | High | Registry |
| `NSWorkspace` posts app launch and terminate notifications. | High | Registry |
| `SCStream` with a single-window filter captures that window's own buffer, so occlusion and other Spaces do not matter. | High | Watch |
| `SCContentFilter` can target an application rather than a window, so new windows of that app are included automatically. | High | Watch |
| Every delivered stream frame is `complete`, changed or not; `idle` never arrives. Minimizing or hiding the target delivers one `suspended` frame with no picture, then nothing until it is back. `complete` says a picture was delivered, not that the app drew one. | Verified in M0 | Change gate, staleness |
| `SCScreenshotManager` (macOS 14) captures a single still of a window or app without opening a stream, in 45–180 ms at 1080 wide. | Verified in M0 | Look |
| Apps that track occlusion (Chrome, Electron, Safari) stop drawing when hidden, minimized, **or on another Space**: Chrome on another Space yields `complete` frames and stills whose content area is blank. Terminals and most native apps keep drawing. | Verified in M0 (Chrome, Ghostty) | Staleness UX |
| Minimized and hidden windows go silent (`suspended`). Another Space is fine for apps that keep drawing and blank for those that don't. Same-Space occlusion not yet tested. | Verified in M0 | Staleness UX |
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
| **Record** | One low-rate stream of the display (decided in M0, see §9) | 1 frame / 2–5 s, gated | Build the memory record. On by default, paused from the menu. |

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

**`voice`** — `PipelineWorker`. macOS audio transport (below) → wake-phrase
user-turn-start strategy → STT → context aggregator → Anthropic LLM with
tools → TTS → transport output → assistant aggregator. Speech services are a
switch: Deepgram and Cartesia by default while the pipeline is being tuned,
or Moonshine and Kokoro on the machine with `--local-speech`, where the LLM
is the only network service and `ANTHROPIC_API_KEY` the only key.

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
10. **Record mode streams the display, not the frontmost window.** Taken
    from the M0 data. It shows what the user actually sees, so the blank
    frames occlusion-aware apps produce off-Space cannot reach the memory
    record; it is one stream that survives focus changes; and it keeps
    side-by-side layouts. The registry tags each observation with the
    frontmost app and window title. Capture at 1280 wide if 1080 makes
    text too small.
11. **Staleness is detected three ways, not one.** A `suspended` frame, no
    frame for a few seconds, or a `complete` frame whose content area is
    blank. Frame status alone is not a freshness signal.

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
