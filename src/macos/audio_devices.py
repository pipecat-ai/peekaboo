#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Audio input devices, from the Core Audio HAL.

Devices are named by their UID, which is stable across launches and reboots
(``BuiltInMicrophoneDevice``, a Bluetooth address for a headset), where the
numeric device id is not. PyObjC has no AudioToolbox wrapper and its
CoreAudio one stops at the constants, so the few calls needed go through
ctypes.
"""

import ctypes
import struct
import warnings
from dataclasses import dataclass
from typing import Optional

_CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_AT = ctypes.CDLL("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]


class _PropertyAddress(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


_SYSTEM_OBJECT = 1
_GLOBAL, _INPUT = _fourcc("glob"), _fourcc("inpt")
_DEVICES, _DEFAULT_INPUT, _DEFAULT_OUTPUT = _fourcc("dev#"), _fourcc("dIn "), _fourcc("dOut")
_NAME, _UID, _STREAMS, _TRANSPORT = _fourcc("lnam"), _fourcc("uid "), _fourcc("stm#"), _fourcc("tran")
_TERMINAL_TYPE = _fourcc("term")
# Terminal types of the input streams voice processing adds to *output*
# devices (its echo reference): unknown on the built-in speakers, headphones
# on a Bluetooth headset. Anything else on an input stream is a capture.
_REFERENCE_TERMINALS = {0, _fourcc("spkr"), _fourcc("hdph"), _fourcc("lfes"), _fourcc("rspk")}
_USB_OUTPUT_TERMINALS = range(0x300, 0x400)
# Aggregate devices Core Audio makes for itself while voice processing runs.
_SYSTEM_AGGREGATES = ("CADefaultDeviceAggregate", "VPAUAggregateAudioDevice")
_CURRENT_DEVICE = 2000  # kAudioOutputUnitProperty_CurrentDevice
_UTF8 = 0x08000100

_CA.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_CA.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_AT.AudioUnitSetProperty.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
_CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_CF.CFRelease.argtypes = [ctypes.c_void_p]

TRANSPORTS = {
    "bltn": "Built-in",
    "blue": "Bluetooth",
    "bleo": "Bluetooth",
    "usb ": "USB",
    "cont": "Continuity",
    "virt": "Virtual",
    "aggr": "Aggregate",
    "hdmi": "HDMI",
    "dprt": "DisplayPort",
    "thun": "Thunderbolt",
    "airp": "AirPlay",
    "pci ": "PCI",
}


@dataclass(frozen=True)
class InputDevice:
    uid: str
    name: str
    transport: str
    """Human name of the transport ("Built-in", "Bluetooth"), or ""."""
    default: bool

    def describe(self) -> dict:
        return {"uid": self.uid, "name": self.name, "transport": self.transport, "default": self.default}


def _prop(obj: int, selector: int, scope: int = _GLOBAL) -> bytes:
    addr = _PropertyAddress(selector, scope, 0)
    size = ctypes.c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0 or size.value == 0:
        return b""
    buf = ctypes.create_string_buffer(size.value)
    if _CA.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf) != 0:
        return b""
    return buf.raw[: size.value]


def _string(obj: int, selector: int) -> str:
    raw = _prop(obj, selector)
    if len(raw) != 8:
        return ""
    ref = struct.unpack("P", raw)[0]
    if not ref:
        return ""
    out = ctypes.create_string_buffer(1024)
    ok = _CF.CFStringGetCString(ref, out, len(out), _UTF8)
    _CF.CFRelease(ref)
    return out.value.decode("utf-8", "replace") if ok else ""


def _has_microphone(dev: int) -> bool:
    """An input stream that is a real capture terminal. While voice
    processing is on, output devices and the system's aggregates grow input
    streams too (the echo canceller's reference), typed as unknown or as the
    speaker they mirror; a microphone's stream says what it is."""
    raw = _prop(dev, _STREAMS, _INPUT)
    for stream in struct.unpack(f"{len(raw) // 4}I", raw):
        term = _prop(stream, _TERMINAL_TYPE)
        if len(term) != 4:
            continue
        kind = struct.unpack("I", term)[0]
        if kind not in _REFERENCE_TERMINALS and kind not in _USB_OUTPUT_TERMINALS:
            return True
    return False


def _device_ids() -> list[int]:
    raw = _prop(_SYSTEM_OBJECT, _DEVICES)
    return list(struct.unpack(f"{len(raw) // 4}I", raw))


def default_input_id() -> int:
    raw = _prop(_SYSTEM_OBJECT, _DEFAULT_INPUT)
    return struct.unpack("I", raw)[0] if len(raw) == 4 else 0


@dataclass(frozen=True)
class OutputDevice:
    uid: str
    name: str
    transport: str


def default_output() -> Optional[OutputDevice]:
    """The system's default output device."""
    raw = _prop(_SYSTEM_OBJECT, _DEFAULT_OUTPUT)
    if len(raw) != 4:
        return None
    dev = struct.unpack("I", raw)[0]
    tran = _prop(dev, _TRANSPORT)
    code = struct.pack(">I", struct.unpack("I", tran)[0]).decode("ascii", "replace") if len(tran) == 4 else ""
    return OutputDevice(uid=_string(dev, _UID), name=_string(dev, _NAME), transport=TRANSPORTS.get(code, ""))


def same_headset(input_uid: str, output_uid: str) -> bool:
    """Whether a microphone and an output are two sides of one Bluetooth
    headset: their UIDs share the address ("AA-BB-…:input", "AA-BB-…:output")."""
    return bool(input_uid) and input_uid.split(":")[0] == output_uid.split(":")[0]


def input_devices() -> list[InputDevice]:
    """Every device with input streams, the system default flagged."""
    default = default_input_id()
    devices = []
    for dev in _device_ids():
        uid = _string(dev, _UID)
        if uid.startswith(_SYSTEM_AGGREGATES) or not _has_microphone(dev):
            continue
        tran = _prop(dev, _TRANSPORT)
        code = struct.pack(">I", struct.unpack("I", tran)[0]).decode("ascii", "replace") if len(tran) == 4 else ""
        devices.append(InputDevice(uid=uid, name=_string(dev, _NAME), transport=TRANSPORTS.get(code, ""), default=dev == default))
    return devices


def input_device_id(uid: str) -> Optional[int]:
    """The device id for a UID, if that device is present with input."""
    for dev in _device_ids():
        if _string(dev, _UID) == uid and _has_microphone(dev):
            return dev
    return None


def create_aggregate(output_uid: str, input_uid: str) -> Optional[int]:
    """A private aggregate device of an output and a microphone, for an I/O
    unit that must play through one device and capture from another (AVAudioEngine's
    input and output nodes share one unit). Returns its device id."""
    import CoreAudio

    description = {
        "uid": f"ai.pipecat.peekaboo.{input_uid}",
        "name": "Peekaboo",
        "subdevices": [{"uid": output_uid}, {"uid": input_uid}],
        "master": output_uid,
        "private": 1,
        "stacked": 0,
    }
    err, device_id = CoreAudio.AudioHardwareCreateAggregateDevice(description, None)
    return device_id if err == 0 and device_id else None


def destroy_aggregate(device_id: int):
    import CoreAudio

    CoreAudio.AudioHardwareDestroyAggregateDevice(device_id)


def pin_input_unit(audio_unit, device_id: int) -> bool:
    """Point an input AudioUnit (``AVAudioInputNode.audioUnit()``) at a
    device. Must be called before the engine starts."""
    import objc

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", objc.ObjCPointerWarning)
        address = getattr(audio_unit, "pointerAsInteger", None)
    if not address:
        return False
    dev = ctypes.c_uint32(device_id)
    return _AT.AudioUnitSetProperty(address, _CURRENT_DEVICE, _GLOBAL, 0, ctypes.byref(dev), 4) == 0
