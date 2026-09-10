<h1><div align="center">
 <img alt="pipecat" width="300px" height="auto" src="https://raw.githubusercontent.com/pipecat-ai/pipecat/main/pipecat.png">
</div></h1>

# 🐈‍⬛ Peekaboo

**A voice assistant for your Mac that remembers your screen.** Built with
[Pipecat](https://github.com/pipecat-ai/pipecat).

Peekaboo sits in the menu bar, listens for its name, and keeps a memory of
every window you have open. Ask it what you were doing, what a window says,
or to tell you when something happens.

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

To run it as an app:

```bash
uv run tools/make_app.py
open dist/Peekaboo.app
```

## 🔧 Built on Pipecat

Peekaboo is a showcase of Pipecat on a desktop:

- **Six workers on one bus**: voice, screen, vision, history, the window agent (a `UIWorker`), and the shell.
- **A native macOS audio transport** on `AVAudioEngine`, with echo cancellation and microphone selection.
- **Local speech** with Moonshine as both recognizer and wake word, and Kokoro as the voice.
- **RTVI end to end**: the window is a web view running `@pipecat-ai/client-js`, driven by voice through UI commands.
- **Vision in a pipeline**: windows flow through a change gate and an image processor before the model, so only what changed is described, at most every 15 seconds per window.

## 🛠️ Development

```bash
uv run --with pytest pytest tests/            # unit tests
uv run evals/run.py --start-bot evals/*.yaml  # headless scenarios
```

`PLAN.md` is the design and the record of what was tried and decided.

## License

BSD 2-Clause, as noted in each source file.
