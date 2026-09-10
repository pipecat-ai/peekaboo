<div align="center">
 <img alt="pipecat" width="300px" height="auto" src="https://raw.githubusercontent.com/pipecat-ai/pipecat/main/pipecat.png">
</div>

# Peekaboo

**Peekaboo is an experimental voice assistant for your Mac that remembers
your screen.** It sits in the menu bar, listens for its name, and keeps a
memory of every window you have open. Ask it what you were doing, what a
window says, or to tell you when something happens.

> "Peekaboo, what was I doing this morning?"

## ✨ What it can do

| | What happens | You say |
|---|---|---|
| 🧠 **Remembers your screen** | Every window is captured as it changes and described, with the text that was on it. | "What was I doing this morning?" |
| 🗣️ **Answers from memory** | A short spoken answer, and the screenshots behind it in its window. | "What PR did I look at yesterday?" |
| 👀 **Looks when asked** | Reads a window right now. | "What does the terminal say?" |
| 🔔 **Watches for you** | Binds a condition to a window and speaks up when it happens. | "Tell me when the build finishes." |
| 📅 **Catches meeting reminders** | Reads any app's notification banner and joins with a word. | "Yes, join it." |
| 🖱️ **Runs its window by voice** | Clicks, navigates, and zooms the timeline as if you had. | "Open the third one." |
| 🔒 **Keeps speech local** | Moonshine hears, Kokoro speaks; nothing leaves your Mac until you say its name. | "Peekaboo." |

## 🧩 Local and cloud

Speech stays on your Mac. Only the two language models run in the cloud,
and only the vision one ever sees a screenshot.

| | Runs | Model |
|---|---|---|
| Wake word and speech recognition | On your Mac | Moonshine |
| Voice | On your Mac | Kokoro |
| Conversation (Voice LLM) | Cloud | Anthropic by default, or OpenAI |
| Reading the screen (Vision LLM) | Cloud | Anthropic by default, or OpenAI |

All of it is chosen in Settings ▸ Models. Nothing you say leaves the Mac
until you say "Peekaboo"; nothing on your screen leaves it except the
frames sent to the Vision LLM to be described.

## 🔧 Built on Pipecat

Peekaboo is a showcase of what [Pipecat](https://github.com/pipecat-ai/pipecat)
can do beyond a single voice pipeline, and above all of its multi-agent
system: several workers with their own pipelines and models cooperating over
one bus as one app.

| Pipecat feature | How Peekaboo uses it |
|---|---|
| **Multi-agent system** | Six workers on one runner and one bus: `voice`, `screen`, `vision`, `history`, the window agent, and the shell. They hand work to each other as jobs and answer with updates and results. |
| **LLM workers** | `voice`, `vision` and `history` are `LLMWorker`s whose tools are `@tool` methods; the window agent is a `UIWorker` that reads the page's accessibility snapshots. |
| **Local transport** | A native macOS audio transport on `AVAudioEngine`, with the OS echo canceller, microphone selection, and RTVI messages in both directions. |
| **Local speech** | Moonshine, a segmented STT service, hears everything and doubles as the wake word; Kokoro speaks. Nothing leaves the Mac until Peekaboo is awake. |
| **Pipecat clients** | The window is a web view running `@pipecat-ai/client-js` over a bridge transport, so voice and mouse drive the same page through UI commands. |
| **Vision pipelines** | Windows flow through a change gate and an image processor before the model, so only what changed is described, at most every 15 seconds per window. |
| **Evals** | Headless scenarios on Pipecat's eval transport check tool routing, screen questions, watchers, and reminders. |

## 📋 Requirements

- macOS 14 or later.
- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- A Pipecat checkout next to this one, at `../pipecat`.
- An Anthropic or OpenAI API key for the two language models. Speech is local and needs none.

## 🚀 Getting started

```bash
uv sync
uv run src/app.py
```

Grant the microphone and Screen Recording when asked, open the window from
the menu bar cat, and paste an Anthropic or OpenAI API key in
Settings ▸ Models. It is stored in your keychain. Then say
"Peekaboo, what's on my screen?"

To run it as an app:

```bash
uv run tools/make_app.py
open dist/Peekaboo.app
```

## 🛠️ Development

```bash
uv run --with pytest pytest tests/            # unit tests
uv run evals/run.py --start-bot evals/*.yaml  # headless scenarios
```

`docs/PLAN.md` is the design and the record of what was tried, measured,
and decided.

## License

BSD 2-Clause, as noted in each source file.
