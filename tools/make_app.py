#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Build ``dist/Peekaboo.app``, a development bundle around this checkout.

The bundle is what gives the process an identity: its own name and icon in
⌘-Tab and the Dock, and its own row in Privacy & Security instead of the
terminal's. AppKit takes both from the bundle that holds the running
executable, so the interpreter itself must live in the bundle: a copy of
the framework's Python binary sits in ``Contents/MacOS``, with a
``pyvenv.cfg`` beside it and ``Contents/lib`` pointing at this checkout's
``.venv``, so it is the project's environment under another roof. The
models and the code stay in the repository; run ``uv sync`` when
dependencies change. Packaging with everything inside is a later step.

    uv run tools/make_app.py            # writes dist/Peekaboo.app
    open dist/Peekaboo.app              # launch it like any app

The launcher appends the app's log to ~/Library/Logs/Peekaboo.log.
"""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
APP = DIST / "Peekaboo.app"
ICON_PNG = ROOT / "src" / "macos" / "assets" / "appicon.png"

BUNDLE_ID = "ai.pipecat.peekaboo"
NAME = "Peekaboo"

LAUNCHER = """#!/bin/zsh
# Peekaboo development launcher: the bundle's own interpreter, the checkout's code.
DIR="${{0:A:h}}"
cd "{root}" || exit 1
mkdir -p "$HOME/Library/Logs"
exec "$DIR/python" src/app.py "$@" >> "$HOME/Library/Logs/Peekaboo.log" 2>&1
"""


def make_icns(png: Path, out: Path):
    """An .icns from one PNG: every size macOS asks for, via sips and iconutil."""
    iconset = out.with_suffix(".iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            subprocess.run(
                ["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)],
                check=True,
                capture_output=True,
            )
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    shutil.rmtree(iconset, ignore_errors=True)


def interpreter() -> Path:
    """The real interpreter behind ``.venv/bin/python``. Framework builds put
    a stub in ``bin`` that execs ``Resources/Python.app/Contents/MacOS/Python``;
    that binary is the one AppKit runs, so it is the one to copy."""
    stub = (ROOT / ".venv" / "bin" / "python").resolve()
    real = stub.parent.parent / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    return real if real.exists() else stub


def main():
    venv = ROOT / ".venv"
    if not (venv / "pyvenv.cfg").exists():
        print("no .venv; run `uv sync` first", file=sys.stderr)
        return 1
    shutil.rmtree(APP, ignore_errors=True)
    macos = APP / "Contents" / "MacOS"
    resources = APP / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    launcher = macos / NAME
    launcher.write_text(LAUNCHER.format(root=str(ROOT)))
    launcher.chmod(0o755)

    # The interpreter, inside the bundle, running the checkout's environment.
    shutil.copy2(interpreter(), macos / "python")
    (macos / "python").chmod(0o755)
    shutil.copy2(venv / "pyvenv.cfg", APP / "Contents" / "pyvenv.cfg")
    (APP / "Contents" / "lib").symlink_to(venv / "lib", target_is_directory=True)

    make_icns(ICON_PNG, resources / "AppIcon.icns")

    info = {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "0.0.1",
        "CFBundleShortVersionString": "0.0.1",
        "CFBundleExecutable": NAME,
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "14.0",
        # A menu bar app: no Dock icon until the window opens (the app
        # switches its activation policy itself).
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Peekaboo listens for its name and for what you ask it.",
        "NSSpeechRecognitionUsageDescription": "Peekaboo turns what you say into requests.",
        "NSAppleEventsUsageDescription": "Peekaboo opens meeting links in your browser.",
    }
    with (APP / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump(info, f)
    (APP / "Contents" / "PkgInfo").write_text("APPL????")
    # An ad-hoc signature gives the bundle a stable identity for the privacy
    # database, so permissions granted to Peekaboo stay with Peekaboo.
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(APP)], check=True, capture_output=True)
    print(f"built {APP.relative_to(ROOT)} around {ROOT} with {interpreter()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
