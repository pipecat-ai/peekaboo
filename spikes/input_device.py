#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Spike: list the input devices and pin an ``AVAudioEngine`` to one of them.

The HAL is reached through ctypes (PyObjC's CoreAudio wrapper has the
constants but no AudioToolbox, and ``AudioUnitSetProperty`` is what sets the
input node's device). Verifies: enumeration with names, UIDs, transport and
input-ness; the default input; setting the input unit's device before the
engine starts and reading the tap at that device's rate.

    uv run spikes/input_device.py            # list, then pin the default
    uv run spikes/input_device.py "MacBook"  # pin the device whose name contains this
"""

import ctypes
import ctypes.util
import struct
import sys
import time

CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
AT = ctypes.CDLL("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")
CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]


class PropertyAddress(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


SYSTEM_OBJECT = 1
GLOBAL, INPUT = fourcc("glob"), fourcc("inpt")
DEVICES, DEFAULT_INPUT = fourcc("dev#"), fourcc("dIn ")
NAME, UID, STREAMS, TRANSPORT = fourcc("lnam"), fourcc("uid "), fourcc("stm#"), fourcc("tran")
CURRENT_DEVICE = 2000  # kAudioOutputUnitProperty_CurrentDevice

CA.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(PropertyAddress), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
CA.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(PropertyAddress), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
AT.AudioUnitSetProperty.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
AT.AudioUnitGetProperty.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
CF.CFRelease.argtypes = [ctypes.c_void_p]


def prop(obj: int, selector: int, scope: int = GLOBAL) -> bytes:
    addr = PropertyAddress(selector, scope, 0)
    size = ctypes.c_uint32(0)
    if CA.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
        return b""
    buf = ctypes.create_string_buffer(size.value)
    if CA.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf) != 0:
        return b""
    return buf.raw[: size.value]


def cfstring(obj: int, selector: int) -> str:
    raw = prop(obj, selector)
    if len(raw) != 8:
        return ""
    ref = struct.unpack("P", raw)[0]
    out = ctypes.create_string_buffer(512)
    ok = CF.CFStringGetCString(ref, out, 512, 0x08000100)  # kCFStringEncodingUTF8
    CF.CFRelease(ref)
    return out.value.decode("utf-8") if ok else ""


def input_devices() -> list[dict]:
    ids = struct.unpack(f"{len(prop(SYSTEM_OBJECT, DEVICES)) // 4}I", prop(SYSTEM_OBJECT, DEVICES))
    default = struct.unpack("I", prop(SYSTEM_OBJECT, DEFAULT_INPUT))[0]
    out = []
    for dev in ids:
        if not prop(dev, STREAMS, INPUT):
            continue
        tran = prop(dev, TRANSPORT)
        out.append(
            {
                "id": dev,
                "name": cfstring(dev, NAME),
                "uid": cfstring(dev, UID),
                "transport": struct.pack(">I", struct.unpack("I", tran)[0]).decode("ascii", "replace") if tran else "",
                "default": dev == default,
            }
        )
    return out


def main():
    devices = input_devices()
    for d in devices:
        print(f"{'*' if d['default'] else ' '} {d['id']:>4} {d['transport']} {d['name']!r} uid={d['uid']}")
    want = sys.argv[1] if sys.argv[1:] else None
    chosen = next((d for d in devices if want and want.lower() in d["name"].lower()), None) or next(d for d in devices if d["default"])
    print(f"pinning to {chosen['name']!r} ({chosen['id']})")

    import AVFoundation as AVF

    engine = AVF.AVAudioEngine.alloc().init()
    node = engine.inputNode()
    import warnings

    import objc

    # The AudioUnit comes back as an opaque pointer PyObjC warns about; its address is what ctypes needs.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", objc.ObjCPointerWarning)
        unit = node.audioUnit()
    ptr = unit.pointerAsInteger
    dev = ctypes.c_uint32(chosen["id"])
    err = AT.AudioUnitSetProperty(ptr, CURRENT_DEVICE, GLOBAL, 0, ctypes.byref(dev), 4)
    print(f"AudioUnitSetProperty(CurrentDevice) -> {err}")
    got = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    AT.AudioUnitGetProperty(ptr, CURRENT_DEVICE, GLOBAL, 0, ctypes.byref(got), ctypes.byref(size))
    print(f"unit now on device {got.value}")

    fmt = node.outputFormatForBus_(0)
    print(f"input format: {fmt.sampleRate()} Hz, {fmt.channelCount()} ch")
    frames = []
    node.installTapOnBus_bufferSize_format_block_(0, 1024, None, lambda buf, when: frames.append(buf.frameLength()))
    engine.prepare()
    ok, error = engine.startAndReturnError_(None)
    print(f"engine start: {ok} {error or ''}")
    time.sleep(1.0)
    engine.stop()
    print(f"tap delivered {sum(frames)} frames in 1 s ({len(frames)} buffers)")


if __name__ == "__main__":
    main()
