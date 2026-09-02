# Evals

Scripted conversations that drive the bot headless, in text mode, and check
what it does. No audio, no judge model: every assertion is deterministic.

```
uv run evals/run.py --start-bot evals/*.yaml
```

That starts a fresh bot per scenario under the eval transport, runs the
scenario, stops the bot, and prints what it spoke. Spoken answers, and the
canned acknowledgement after a vision call ("One moment."), bypass the LLM, so
the harness cannot assert on them; the runner reads them from the bot log
instead, and flags tracebacks or errors in it. Logs land in `evals/logs/`.

| Scenario | What it checks |
|---|---|
| `tool_routing.yaml` | A screen question calls `look` with the window the user named as the target, a "tell me when" calls `watch`, a general question calls nothing. |
| `screen_question.yaml` | With a fixture screenshot registered, a look is answered from the picture, the store gets the observation, and a past-tense question goes to the history worker. |
| `meeting.yaml` | A meeting notification appears on screen; the reminder is spoken unprompted and saying yes calls `join_meeting`. |
| `watch.yaml` | "Tell me when the terminal says finished" calls `watch`; a frame with the build still running says nothing, a frame that says FINISHED is announced (in the spoken list), and "stop watching" calls `unwatch`. |

To run against a bot you started yourself, drop `--start-bot`:

```
uv run src/bot.py -t eval
uv run evals/run.py evals/tool_routing.yaml
```

Fixture screenshots are generated images under `evals/fixtures/`. The eval
transport serves the registered image for every screen frame request, so a
scenario sees a static screen unless a later turn registers another image.
