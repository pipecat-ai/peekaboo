#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from links import find_join_url  # noqa: E402


def test_prefers_meeting_hosts_over_other_links():
    text = "Agenda: https://example.com/notes and join at https://zoom.us/j/123?pwd=x. Thanks"
    assert find_join_url(text) == "https://zoom.us/j/123?pwd=x"


def test_accepts_bare_hosts_as_banners_show_them():
    assert find_join_url("Standup · 10:00 · meet.google.com/abc-defg-hij") == (
        "https://meet.google.com/abc-defg-hij"
    )
    assert find_join_url("Join: zoom.us/j/987654321") == "https://zoom.us/j/987654321"


def test_searches_all_texts_and_handles_nothing():
    assert find_join_url("Room 4", None, "https://teams.microsoft.com/l/meetup-join/xyz") == (
        "https://teams.microsoft.com/l/meetup-join/xyz"
    )
    assert find_join_url(None, "", "no links here") is None
