#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Which models Peekaboo runs on, chosen in Settings.

Two language models: the *voice* one is the conversation (the voice worker,
and with it the window agent and the history answers, which are its
helpers), the *vision* one describes frames and answers "look" (the screen
and vision workers). Each is a provider plus a model; a provider has one
API key, entered in Settings and kept in the keychain, nowhere else. Speech
stays on the machine: Moonshine hears (and is the wake word), Kokoro speaks.

The app reads the choices once at launch (:func:`configure`); a change in
Settings applies at the next launch.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger

ANTHROPIC = "anthropic"
OPENAI = "openai"

PROVIDERS = {
    ANTHROPIC: {"name": "Anthropic"},
    OPENAI: {"name": "OpenAI"},
}

# Suggested models per provider, best for a spoken conversation first.
# Free text is accepted too: a newer model name works without a code change.
MODELS = {
    ANTHROPIC: ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
    OPENAI: ["gpt-4.1", "gpt-4.1-mini", "gpt-4o"],
}

MOONSHINE_MODELS = ["tiny", "base", "tiny-streaming", "base-streaming", "small-streaming", "medium-streaming"]

DEFAULT_MODEL = {ANTHROPIC: "claude-haiku-4-5", OPENAI: "gpt-4.1"}

# Kokoro's voices, from its voices file when it is on disk, else this list.
KOKORO_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova", "af_river",
    "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck",
    "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]

DEFAULT_MODEL_SETTINGS: dict[str, Any] = {
    "voice_llm": {"provider": ANTHROPIC, "model": DEFAULT_MODEL[ANTHROPIC]},
    "vision_llm": {"provider": ANTHROPIC, "model": DEFAULT_MODEL[ANTHROPIC]},
    "stt_model": "medium-streaming",
    "tts_voice": "af_heart",
}


@dataclass(frozen=True)
class ModelChoice:
    provider: str
    model: str

    @classmethod
    def from_setting(cls, value: Any, fallback: dict) -> "ModelChoice":
        value = value if isinstance(value, dict) else {}
        provider = str(value.get("provider") or fallback["provider"])
        if provider not in PROVIDERS:
            provider = fallback["provider"]
        model = str(value.get("model") or "").strip() or DEFAULT_MODEL[provider]
        return cls(provider, model)


@dataclass(frozen=True)
class Models:
    voice: ModelChoice = field(default_factory=lambda: ModelChoice(ANTHROPIC, DEFAULT_MODEL[ANTHROPIC]))
    vision: ModelChoice = field(default_factory=lambda: ModelChoice(ANTHROPIC, DEFAULT_MODEL[ANTHROPIC]))
    stt_model: str = "medium-streaming"
    tts_voice: str = "af_heart"

    @classmethod
    def from_settings(cls, settings: dict) -> "Models":
        return cls(
            voice=ModelChoice.from_setting(settings.get("voice_llm"), DEFAULT_MODEL_SETTINGS["voice_llm"]),
            vision=ModelChoice.from_setting(settings.get("vision_llm"), DEFAULT_MODEL_SETTINGS["vision_llm"]),
            stt_model=str(settings.get("stt_model") or DEFAULT_MODEL_SETTINGS["stt_model"]),
            tts_voice=str(settings.get("tts_voice") or DEFAULT_MODEL_SETTINGS["tts_voice"]),
        )

    def providers(self) -> set[str]:
        return {self.voice.provider, self.vision.provider}


_current = Models()


def configure(models: Models):
    """What the workers build their services from, set once at launch."""
    global _current
    _current = models
    logger.info(
        f"models: voice {models.voice.provider}/{models.voice.model}, vision {models.vision.provider}/{models.vision.model}, "
        f"speech moonshine/{models.stt_model} and kokoro/{models.tts_voice}"
    )


def current() -> Models:
    return _current


def api_key(provider: str) -> Optional[str]:
    """The provider's key from the keychain, or None."""
    try:
        from macos import keychain

        return keychain.get(provider)
    except Exception:  # noqa: BLE001 - no keychain on this platform
        return None


def missing_keys(models: Optional[Models] = None) -> list[str]:
    """Providers in use with no key anywhere."""
    models = models or _current
    return [p for p in sorted(models.providers()) if not api_key(p)]


def make_llm(
    choice: ModelChoice,
    *,
    name: str,
    system_instruction: Optional[str] = None,
    max_tokens: Optional[int] = None,
    json_schema: Optional[dict] = None,
    read_timeout_secs: Optional[float] = None,
    **kwargs,
):
    """An LLM service for a choice. No extended thinking anywhere: every
    answer is spoken or describes a picture, and speed matters more. The
    client read timeout is an Anthropic feature; other providers get the
    model and the prompt. ``json_schema`` asks for structured output in
    that shape, in each provider's own way."""
    key = api_key(choice.provider)
    if choice.provider == OPENAI:
        from pipecat.services.openai.llm import OpenAILLMService

        settings: dict[str, Any] = {"model": choice.model}
        if system_instruction is not None:
            settings["system_instruction"] = system_instruction
        if max_tokens is not None:
            settings["max_tokens"] = max_tokens
        if json_schema is not None:
            settings["extra"] = {
                "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": json_schema, "strict": True}}
            }
        return OpenAILLMService(name=name, api_key=key, settings=OpenAILLMService.Settings(**settings), **kwargs)

    from pipecat.services.anthropic.llm import AnthropicLLMService

    settings = {"model": choice.model}
    if system_instruction is not None:
        settings["system_instruction"] = system_instruction
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens
    if json_schema is not None:
        # Structured outputs: output_config.format, no beta header.
        settings["extra"] = {"extra_body": {"output_config": {"format": {"type": "json_schema", "schema": json_schema}}}}
    if read_timeout_secs is not None:
        import httpx
        from anthropic import AsyncAnthropic

        kwargs["client"] = AsyncAnthropic(api_key=key, timeout=httpx.Timeout(read_timeout_secs, connect=10.0), max_retries=1)
    return AnthropicLLMService(name=name, api_key=key, settings=AnthropicLLMService.Settings(**settings), **kwargs)


def kokoro_voices() -> list[str]:
    """The voices Kokoro has on this machine (its voices file), else the known list."""
    path = Path(os.path.expanduser("~/.cache/pipecat/kokoro-onnx/voices-v1.0.bin"))
    if path.exists():
        try:
            import numpy as np

            return sorted(np.load(path).files)
        except Exception:  # noqa: BLE001 - the list below then
            pass
    return list(KOKORO_VOICES)


def describe() -> dict:
    """What Settings shows: the choices, the suggestions, which keys exist."""
    return {
        "providers": [
            {"id": p, "name": v["name"], "models": MODELS[p], "has_key": bool(api_key(p))}
            for p, v in PROVIDERS.items()
        ],
        "moonshine_models": MOONSHINE_MODELS,
        "kokoro_voices": kokoro_voices(),
    }
