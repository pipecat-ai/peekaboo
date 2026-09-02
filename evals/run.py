#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Run eval scenarios against the bot.

    uv run evals/run.py --start-bot evals/*.yaml

starts a fresh bot per scenario under the eval transport, waits for it, runs
the scenario, and stops it, then prints what the bot spoke, which the harness
cannot see because spoken answers bypass the LLM. Bot logs land in
evals/logs/<scenario>.log.

Without --start-bot the scenarios run against a bot you started yourself with
`uv run src/bot.py -t eval`.
"""

import argparse
import asyncio
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from pipecat.evals.harness import EvalSession
from pipecat.evals.scenario import EvalScenario

BOT_URL = "ws://localhost:7860"
BOT_PORT = 7860
ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "evals" / "logs"

_SPOKEN = re.compile(r"\| [^ ]+ - (voice: (?:saying|prompting|would open|opened)[^\n]*)")


def _port_listening(port: int) -> bool:
    """Whether something is listening on a local TCP port.

    Asks the OS instead of connecting, so the bot's websocket server does not
    log a half-open connection for every probe: the kernel's socket table on
    Linux, ``lsof`` on macOS. Falls back to a connect probe where neither
    works.
    """
    try:
        with open("/proc/net/tcp") as f:
            next(f)
            for line in f:
                local, state = line.split()[1], line.split()[3]
                if state == "0A" and int(local.split(":")[1], 16) == port:
                    return True
        return False
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        import socket

        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", port)) == 0


async def wait_for_bot(port: int = BOT_PORT, timeout: float = 90) -> None:
    """Block until the bot's websocket server is listening."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not _port_listening(port):
        if loop.time() > deadline:
            raise TimeoutError(f"bot not listening on port {port} after {timeout}s")
        await asyncio.sleep(0.5)


async def wait_for_port_free(port: int = BOT_PORT, timeout: float = 15) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _port_listening(port):
        if loop.time() > deadline:
            raise TimeoutError(f"port {port} still busy after {timeout}s")
        await asyncio.sleep(0.25)


class Bot:
    """A bot process under the eval transport, logging to a file."""

    def __init__(self, log_path: Path, env: dict[str, str]):
        self._log_path = log_path
        self._env = env
        self._proc: subprocess.Popen | None = None
        self._log = None

    async def __aenter__(self):
        await wait_for_port_free()
        LOGS.mkdir(parents=True, exist_ok=True)
        self._log = open(self._log_path, "w")
        self._proc = subprocess.Popen(
            ["uv", "run", "src/bot.py", "-t", "eval"],
            cwd=ROOT,
            env={**os.environ, **self._env},
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        await wait_for_bot()
        return self

    async def __aexit__(self, *exc):
        if self._proc and self._proc.poll() is None:
            # Give the bot a moment to finish speaking anything queued: a
            # watch hit needs a capture, an analysis, and the settle time.
            await asyncio.sleep(6)
            os.killpg(self._proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.to_thread(self._proc.wait), timeout=10)
            except asyncio.TimeoutError:
                os.killpg(self._proc.pid, signal.SIGKILL)
        if self._log:
            self._log.close()

    def spoken(self) -> list[str]:
        try:
            text = self._log_path.read_text()
        except OSError:
            return []
        return [m.group(1) for m in _SPOKEN.finditer(text)]

    def problems(self) -> list[str]:
        try:
            lines = self._log_path.read_text().splitlines()
        except OSError:
            return []
        wanted = re.compile(r"Traceback|Exception|\| ERROR ", re.I)
        return [line[:160] for line in lines if wanted.search(line)]


def report(result, scenario) -> None:
    status = "PASS" if result.passed else "FAIL"
    print(f"{status} {scenario.name} ({result.duration_ms} ms)")
    for turn in result.turns:
        print(f"  turn {turn.turn_index}: {turn.status} ({turn.duration_ms} ms)")
        for failure in turn.failures:
            print(f"    ! {failure}")
    if not result.passed:
        print("  events seen:")
        for event in result.events_seen:
            name = event.get("event") or event.get("type") or "?"
            detail = event.get("text") or event.get("name") or event.get("data") or ""
            print(f"    {name}: {str(detail)[:100]}")


async def run_scenario(path: str, *, start_bot: bool, env: dict[str, str]) -> bool:
    scenario = EvalScenario.load(path)
    if not start_bot:
        await wait_for_bot()
        result = await EvalSession.from_scenario(scenario, BOT_URL).run()
        report(result, scenario)
        return result.passed

    async with Bot(LOGS / f"{scenario.name}.log", env) as bot:
        result = await EvalSession.from_scenario(scenario, BOT_URL).run()
    report(result, scenario)
    spoken = bot.spoken()
    if spoken:
        print("  spoken:")
        for line in spoken:
            print(f"    {line[:140]}")
    problems = bot.problems()
    if problems:
        print("  problems in the bot log:")
        for line in problems[:8]:
            print(f"    {line}")
    return result.passed and not problems


async def main(args: argparse.Namespace) -> int:
    env = dict(item.split("=", 1) for item in args.env)
    failed = 0
    for path in args.scenarios:
        ok = await run_scenario(path, start_bot=args.start_bot, env=env)
        failed += 0 if ok else 1
    print(f"\n{len(args.scenarios) - failed}/{len(args.scenarios)} scenarios passed")
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("scenarios", nargs="+", help="scenario YAML files")
    parser.add_argument(
        "--start-bot", action="store_true", help="start a fresh bot per scenario"
    )
    parser.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE", help="environment for the bot"
    )
    sys.exit(asyncio.run(main(parser.parse_args())))
