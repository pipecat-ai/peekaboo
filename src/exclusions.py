#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Apps Peekaboo never records.

A setting: a list of ``{"bundle_id", "name"}``. Password managers and the
system's own secret stores are in it from the start; anything else is the
user's choice, made in Settings ▸ Recording from the apps that are running.
An excluded app's windows are left out of the registry, so nothing captures,
describes, watches, or lists them, and they are cut out of the screen still.
"""

from typing import Any

DEFAULT_EXCLUDED_APPS: list[dict[str, str]] = [
    {"bundle_id": "com.1password.1password", "name": "1Password"},
    {"bundle_id": "com.agilebits.onepassword7", "name": "1Password 7"},
    {"bundle_id": "com.bitwarden.desktop", "name": "Bitwarden"},
    {"bundle_id": "com.lastpass.LastPass", "name": "LastPass"},
    {"bundle_id": "com.dashlane.dashlanephonefinal", "name": "Dashlane"},
    {"bundle_id": "org.keepassxc.keepassxc", "name": "KeePassXC"},
    {"bundle_id": "in.sinew.Enpass-Desktop", "name": "Enpass"},
    {"bundle_id": "com.nordpass.mac", "name": "NordPass"},
    {"bundle_id": "me.proton.pass.electron", "name": "Proton Pass"},
    {"bundle_id": "com.apple.Passwords", "name": "Passwords"},
    {"bundle_id": "com.apple.keychainaccess", "name": "Keychain Access"},
]


def normalize(value: Any) -> list[dict[str, str]]:
    """The setting as a clean list: bundle ids once each, names kept."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            item = {"bundle_id": item, "name": item}
        if not isinstance(item, dict):
            continue
        bundle_id = str(item.get("bundle_id") or "").strip()
        if not bundle_id or bundle_id in seen:
            continue
        seen.add(bundle_id)
        out.append({"bundle_id": bundle_id, "name": str(item.get("name") or bundle_id).strip()})
    return out


def bundle_ids(value: Any) -> set[str]:
    return {item["bundle_id"] for item in normalize(value)}
