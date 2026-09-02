#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import re
from typing import Optional

# Links that open a meeting, in the order we'd rather find them.
_JOIN_PATTERNS = [
    re.compile(r"(?:https?://)?[\w.-]*zoom\.us/j/[^\s<>\"']+"),
    re.compile(r"(?:https?://)?meet\.google\.com/[a-z0-9-]+"),
    re.compile(r"(?:https?://)?teams\.microsoft\.com/l/meetup-join/[^\s<>\"']+"),
    re.compile(r"(?:https?://)?[\w.-]*webex\.com/[^\s<>\"']+"),
    re.compile(r"(?:https?://)?[\w.-]*daily\.co/[^\s<>\"']+"),
    re.compile(r"https?://[^\s<>\"']+"),
]


def find_join_url(*texts: Optional[str]) -> Optional[str]:
    """The first link in the texts that looks like it joins a meeting.

    Known meeting hosts win over any other URL, wherever they appear, and
    are accepted without a scheme since a banner rarely shows one.
    """
    haystack = "\n".join(t for t in texts if t)
    for pattern in _JOIN_PATTERNS:
        match = pattern.search(haystack)
        if match:
            url = match.group(0).rstrip(".,;)")
            if not url.startswith("http"):
                url = "https://" + url
            return url
    return None
