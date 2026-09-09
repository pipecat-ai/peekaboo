#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""API keys in the login keychain, where a Mac app keeps its secrets.

Through the ``security`` tool rather than the Security framework: one
binary, a generic-password item per provider, and an item it made is one it
may read back without a prompt, from the terminal or the bundle alike.
"""

import subprocess
from typing import Optional

ACCOUNT = "peekaboo"
SERVICE_PREFIX = "ai.pipecat.peekaboo"


def _service(name: str) -> str:
    return f"{SERVICE_PREFIX}.{name}"


def get(name: str) -> Optional[str]:
    """The secret stored under ``name``, or None."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", ACCOUNT, "-s", _service(name), "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n") or None


def set(name: str, value: str) -> bool:
    """Store (or replace) the secret under ``name``."""
    result = subprocess.run(
        ["security", "add-generic-password", "-a", ACCOUNT, "-s", _service(name), "-w", value, "-U"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def delete(name: str) -> bool:
    result = subprocess.run(
        ["security", "delete-generic-password", "-a", ACCOUNT, "-s", _service(name)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
