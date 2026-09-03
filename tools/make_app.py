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
the framework's Python binary sits in ``Contents/MacOS`` and a
``sitecustomize`` module adds this checkout's ``.venv`` packages, so it is
the project's environment under another roof. Nothing in the bundle points
outside it, which keeps the code seal, and with it the permission grants,
valid. The models and the code stay in the repository; run ``uv sync`` when
dependencies change. Packaging with everything inside is a later step.

    uv run tools/make_app.py            # writes dist/Peekaboo.app
    open dist/Peekaboo.app              # launch it like any app

The app's log goes to ~/Library/Logs/Peekaboo.log.

The bundle is signed with a local certificate, "Peekaboo Dev", created in the
login keychain on first use, so the privacy database keeps recognising the
app across rebuilds. Grant Screen Recording and the microphone to Peekaboo
once; they stay granted.
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

# The bundle's executable is the interpreter itself, under the app's name:
# the window server places a status item by the app's declared executable,
# and a shell script that execs into Python left the item parked at the
# origin (M6 finding). With no script to pass, the app is started from a
# ``sitecustomize`` module that Python imports at startup, found through
# ``PYTHONPATH`` set in the bundle's ``LSEnvironment``.
SITECUSTOMIZE = """# Peekaboo development bundle: start the app when the interpreter is run bare.
import os
import runpy
import sys

root = os.environ.get("PEEKABOO_ROOT")
if root:
    # The checkout's environment, without a venv layout in the bundle: a
    # symlink to it inside the bundle broke the code seal.
    import site

    site.addsitedir(os.path.join(root, ".venv", "lib", "python%d.%d" % sys.version_info[:2], "site-packages"))
if root and not sys.argv[1:]:
    os.chdir(root)
    log_dir = os.path.expanduser("~/Library/Logs")
    os.makedirs(log_dir, exist_ok=True)
    log = open(os.path.join(log_dir, "Peekaboo.log"), "a", buffering=1)
    sys.stdout = sys.stderr = log
    # PEEKABOO_MAIN overrides the entry script, for probing the bundle.
    sys.argv = [os.environ.get("PEEKABOO_MAIN") or os.path.join(root, "src", "app.py")]
    sys.path.insert(0, os.path.join(root, "src"))
    runpy.run_path(sys.argv[0], run_name="__main__")
    sys.exit(0)
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


SIGNING_IDENTITY = "Peekaboo Dev"


def signing_identity() -> str:
    """The local code-signing certificate, made if it does not exist yet;
    ad hoc ("-") if that fails."""
    found = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True).stdout
    if SIGNING_IDENTITY in found:
        return SIGNING_IDENTITY
    work = DIST / "signing"
    work.mkdir(parents=True, exist_ok=True)
    cnf = work / "ext.cnf"
    cnf.write_text(
        "[req]\ndistinguished_name = dn\nx509_extensions = v3\nprompt = no\n"
        f"[dn]\nCN = {SIGNING_IDENTITY}\n"
        "[v3]\nkeyUsage = critical, digitalSignature\nextendedKeyUsage = critical, codeSigning\n"
        "basicConstraints = critical, CA:false\nsubjectKeyIdentifier = hash\n"
    )
    keychain = str(Path.home() / "Library" / "Keychains" / "login.keychain-db")
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-keyout", str(work / "key.pem"), "-out", str(work / "cert.pem"), "-config", str(cnf)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["openssl", "pkcs12", "-export", "-legacy", "-inkey", str(work / "key.pem"), "-in", str(work / "cert.pem"), "-out", str(work / "dev.p12"), "-passout", "pass:peekaboo", "-name", SIGNING_IDENTITY],
            check=True, capture_output=True,
        )
        subprocess.run(["security", "import", str(work / "dev.p12"), "-k", keychain, "-P", "peekaboo", "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"], check=True, capture_output=True)
        subprocess.run(["security", "add-trusted-cert", "-r", "trustRoot", "-p", "codeSign", "-k", keychain, str(work / "cert.pem")], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"could not make a signing certificate ({e}); signing ad hoc", file=sys.stderr)
        return "-"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    found = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True).stdout
    return SIGNING_IDENTITY if SIGNING_IDENTITY in found else "-"


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

    # The interpreter, inside the bundle under the app's name. It finds its
    # standard library through the framework it is linked against; the
    # checkout's packages are added by sitecustomize. Nothing in the bundle
    # points outside it, so the code seal stays valid.
    shutil.copy2(interpreter(), macos / NAME)
    (macos / NAME).chmod(0o755)
    (resources / "sitecustomize.py").write_text(SITECUSTOMIZE)

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
        # How the bare interpreter finds and starts the app (see SITECUSTOMIZE).
        # PYTHONDONTWRITEBYTECODE: a .pyc written into Resources after signing
        # would break the seal, and with it the permission grants.
        "LSEnvironment": {"PYTHONPATH": str(resources), "PEEKABOO_ROOT": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "PEEKABOO_LOG": "DEBUG"},
    }
    with (APP / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump(info, f)
    (APP / "Contents" / "PkgInfo").write_text("APPL????")
    # Signed with a local certificate so the privacy database recognises the
    # app across rebuilds: an ad-hoc signature changes with every build, and
    # each build then had to be granted Screen Recording and the microphone
    # again. The certificate is created on first use.
    identity = signing_identity()
    subprocess.run(["codesign", "--force", "--deep", "--sign", identity, str(APP)], check=True, capture_output=True)
    # LaunchServices keeps the previous build's registration for the same
    # path and then refuses to open the new one (error -600) until told.
    lsregister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if Path(lsregister).exists():
        subprocess.run([lsregister, "-f", str(APP)], check=False, capture_output=True)
    print(f"built {APP.relative_to(ROOT)} around {ROOT} with {interpreter()}, signed as {identity}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
