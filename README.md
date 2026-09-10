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

- 🧠 **Remembers your screen.** Every window is captured as it changes and described, with the text that was on it.
- 🗣️ **Answers from memory.** "What PR did I look at yesterday?" gets a short spoken answer and the screenshots behind it.
- 👀 **Looks when asked.** "What does the terminal say?" reads the window right now.
- 🔔 **Watches for you.** "Tell me when the build finishes" or "when I get new Slack messages", and it speaks up.
- 📅 **Catches meeting reminders** from any app's notification, and joins with a word.
- 🖱️ **Runs its window by voice.** "Open the third one", "show me the timeline from last Friday", "go to Settings".
- 🔒 **Keeps speech local.** Moonshine hears, Kokoro speaks, nothing leaves your Mac until you say its name.

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
- **Vision in a pipeline**: a change gate and an image processor that bound cost per window.

## 🛠️ Development

```bash
uv run --with pytest pytest tests/            # unit tests
uv run evals/run.py --start-bot evals/*.yaml  # headless scenarios
```

`PLAN.md` is the design and the record of what was tried and decided.

## License

BSD 2-Clause, as noted in each source file.
