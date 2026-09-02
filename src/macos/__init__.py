#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The macOS layer: everything that talks to the OS.

Capture (ScreenCaptureKit), the window registry, the audio transport
(AVAudioEngine), and permissions. Nothing outside this package and
``sources/macos.py`` imports pyobjc.
"""
