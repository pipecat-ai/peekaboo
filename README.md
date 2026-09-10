<h1><div align="center">
 <img alt="pipecat" width="300px" height="auto" src="https://raw.githubusercontent.com/pipecat-ai/pipecat/main/pipecat.png">
</div></h1>

# 🐈‍⬛ Peekaboo: a voice assistant that remembers your screen

**Peekaboo** lives in your Mac's menu bar, listens for its name, and remembers
what you have seen. It captures every window as it changes, describes what
was there, and keeps the pictures. Later you ask, out loud or in its window,
and it answers from memory: "What was I doing at eleven?", "What PR did I
look at yesterday?", "Tell me when I get new messages on Slack." It reads the
screen on request, watches the windows you point it at, speaks up when a
meeting reminder pops up, and lets you drive its window by voice: "open the
third one", "show me the timeline from last Friday".

Peekaboo is a demo of what [Pipecat](https://github.com/pipecat-ai/pipecat)
can do on a desktop: several pipelines working as one app, a native audio
transport, local speech, and a web view that is a Pipecat client. It is a
Python app end to end.

> Status: in development, macOS only. Expect rough edges; see `PLAN.md` for
> where it is going and what has been verified.

## What it does

- **Remembers.** Every open window is captured as it changes, a still of the
  whole screen with it, and a vision model writes down what was on it, with
  the legible text. The record is searchable by voice or from the window.
- **Answers about the past.** "What was I working on this morning?" is
  answered in a couple of spoken sentences, and the window shows the
  screenshots behind the answer. Every question is kept under Searches.
- **Looks on request.** "What does the terminal say?" reads the window now.
- **Watches.** "Tell me when the build finishes" binds a condition to a
  window, or to every window and notification, and speaks up when it happens.
  Watchers follow their app if the window closes and comes back.
- **Reminds.** Meeting banners from any app are read as they appear, and the
  join link is a "join it" away.
- **Is driven by voice.** The window is a Pipecat client: "open the first
  one", "go to the timeline", "what's the third screenshot about", "zoom in
  on three PM", "open Settings" work like clicks.
- **Stays quiet.** It only speaks unprompted for a reminder or a watch hit,
  and never while you are talking.

## What it shows off in Pipecat

- **Workers, a bus, and jobs.** Six workers on one runner: `voice` (the
  conversation), `screen` (capture, change detection, descriptions,
  watchers), `vision` (look answers), `history` (search answers with
  extended thinking), `ui` (the window agent, a Pipecat `UIWorker`), and
  `shell` (the menu bar and window plumbing, a `BaseUIWorker`). They talk
  over the bus with jobs and updates, never through shared state.
- **A native audio transport.** `MacAudioTransport` runs on one
  `AVAudioEngine` with the OS echo canceller, picks the microphone, keeps
  working through device changes, and carries RTVI messages both ways.
- **Local speech.** Moonshine, a Pipecat segmented STT service, hears
  everything and is the wake word too, through a phonetic wake gate; nothing
  you say leaves the machine until Peekaboo is awake. Kokoro speaks.
- **RTVI end to end.** The window is a web view running
  `@pipecat-ai/client-js` over a bridge transport: its data calls are client
  messages answered by a worker, and everything the app pushes is a
  `ui-command`. The window agent works from the page's accessibility
  snapshots and acts through the same commands.
- **Vision in a pipeline.** A frame source at the head of the screen
  pipeline, a change gate, and an image processor that bounds cost per
  window, sends only the changed region enlarged, and keeps a watch list.

## Requirements

- macOS 14 or later.
- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- A Pipecat checkout next to this one, at `../pipecat`, on `main`. Peekaboo
  tracks Pipecat's development branch.
- An Anthropic or OpenAI API key for the two language models. Speech is
  local and needs no key.

## Getting started

```bash
uv sync
uv run src/app.py
```

The cat appears in the menu bar. The first run asks for the microphone and
for Screen Recording; the app relaunches itself once the latter is granted.
Open the window from the menu (or say "Peekaboo, open the window"), go to
Settings ▸ Models, and paste your API key. It is kept in your login keychain;
Peekaboo reads no `.env`.

Then talk to it: "Peekaboo, what's on my screen?" A bare "Peekaboo" gets a
"Yes?". Everything else it can do is in the window: Ask, Searches, Timeline,
Watchers, Settings.

### As an app

```bash
uv run tools/make_app.py
open dist/Peekaboo.app
```

The bundle wraps this checkout: it gives Peekaboo its own name, icon, and
row in Privacy & Security, signed with a local certificate so permissions
survive rebuilds. The log is at `~/Library/Logs/Peekaboo.log`.

### Settings worth knowing

- **Microphone.** With Bluetooth earbuds as the output, choose the built-in
  microphone here; otherwise macOS uses the earbuds' hands-free mic and they
  drop to low quality.
- **Echo cancellation.** Lets Peekaboo hear you while it speaks. While it is
  on, other apps recording the microphone get silence: turn it off to
  record your screen with another app, and wear headphones.
- **Models.** The Moonshine model, the Kokoro voice, and the Voice and
  Vision LLMs by provider. Changes apply on the next launch; a Restart
  button appears.

## Privacy

Speech recognition and synthesis run on the machine. What reaches a cloud
service is what the language models need: window frames for descriptions,
and your questions with the memories that answer them. Frames and
descriptions are stored under `~/Library/Application Support/Peekaboo`;
Peekaboo's own window is never recorded. API keys live in the keychain.

## Development

```bash
uv run --with pytest pytest tests/          # unit tests
uv run evals/run.py --start-bot evals/*.yaml # headless scenarios (text in, speech out)
uv run src/app.py -v                         # debug log; -vv for trace
```

Dev hooks on `src/app.py`: `--open-memories`, `--window-request TEXT` (hand
a sentence to the window agent), `--memories-eval JS` (run JavaScript in
the page), `--snapshot-memories PNG`, `--record-mic DIR` (write the
microphone to a WAV, then `uv run tools/transcribe_wav.py` to run Moonshine
over it), `--stt-model`, `--tts cartesia`, `--stt deepgram`.

`PLAN.md` is the design and the record of what was tried, measured, and
decided. `spikes/` holds the small programs that settled the macOS facts
the design relies on.

## License

BSD 2-Clause, as noted in each source file.
