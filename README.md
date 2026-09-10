<div align="center">
 <img alt="pipecat" width="300px" height="auto" src="https://raw.githubusercontent.com/pipecat-ai/pipecat/main/pipecat.png">
</div>

# Peekaboo

**Peekaboo is an experimental voice assistant for your Mac that remembers
your screen**, built to showcase [Pipecat](https://github.com/pipecat-ai/pipecat)
and its multi-agent system. It sits in the menu bar, listens for its name,
and keeps a memory of every window you have open. Ask it what you were
doing, what a window says, or to tell you when something happens.

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

## 🔧 Built on Pipecat

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
- An Anthropic or OpenAI API key. Speech is local and needs none.

## 🚀 Getting started

```bash
uv sync
uv run src/app.py
```

Grant the microphone and Screen Recording when asked, open the window from
the menu bar cat, and paste an Anthropic or OpenAI API key in
Settings ▸ Models. It is stored in your keychain. Then say
"Peekaboo, what's on my screen?"

## 🛠️ Development

```bash
uv run --with pytest pytest tests/            # unit tests
uv run evals/run.py --start-bot evals/*.yaml  # headless scenarios
```
