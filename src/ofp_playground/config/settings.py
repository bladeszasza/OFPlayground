"""Configuration management for OFP Playground."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import tomli_w

CONFIG_DIR = Path.home() / ".ofp-playground"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class FloorConfig:
    policy: str = "sequential"
    max_agents: int = 10
    timeout_seconds: int = 30


@dataclass
class ApiKeysConfig:
    anthropic: Optional[str] = None
    openai: Optional[str] = None
    google: Optional[str] = None
    huggingface: Optional[str] = None


@dataclass
class DefaultsConfig:
    llm_model_anthropic: str = "claude-haiku-4-5-20251001"          # smallest Claude
    vision_model_anthropic: str = "claude-haiku-4-5-20251001"       # Claude vision (image-to-text)
    llm_model_openai: str = "gpt-5.4-nano"                          # smallest GPT-5.4
    image_model_openai: str = "gpt-5"                               # model used with image_generation tool
    video_model_openai: str = "sora-2"                              # Sora video generation
    vision_model_openai: str = "gpt-4o-mini"                        # OpenAI vision (image-to-text)
    llm_model_google: str = "gemini-3.1-flash-lite-preview"          # default Gemini text model
    image_model_google: str = "gemini-3.1-flash-image-preview"      # Gemini image generation (Nano Banana)
    vision_model_google: str = "gemini-3-flash-preview"              # Gemini vision (image-to-text)
    music_model_google: str = "models/lyria-realtime-exp"            # Lyria RealTime music generation
    video_model_google: str = "veo-3.1-generate-preview"             # Veo video generation
    llm_model_huggingface: str = "MiniMaxAI/MiniMax-M2.5"           # stronger default for HF debates
    relevance_filter: bool = True


@dataclass
class Settings:
    floor: FloorConfig = field(default_factory=FloorConfig)
    api_keys: ApiKeysConfig = field(default_factory=ApiKeysConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)

    def get_anthropic_key(self) -> Optional[str]:
        return self.api_keys.anthropic or os.environ.get("ANTHROPIC_API_KEY")

    def get_openai_key(self) -> Optional[str]:
        return self.api_keys.openai or os.environ.get("OPENAI_API_KEY")

    def get_google_key(self) -> Optional[str]:
        return self.api_keys.google or os.environ.get("GOOGLE_API_KEY")

    def get_huggingface_key(self) -> Optional[str]:
        return (
            self.api_keys.huggingface
            or os.environ.get("HF_API_KEY")
            or os.environ.get("HF_TOKEN")
        )

    @classmethod
    def load(cls) -> "Settings":
        settings = cls()
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "rb") as f:
                data = tomllib.load(f)
            if "floor" in data:
                for k, v in data["floor"].items():
                    if hasattr(settings.floor, k):
                        setattr(settings.floor, k, v)
            if "api_keys" in data:
                for k, v in data["api_keys"].items():
                    if hasattr(settings.api_keys, k):
                        setattr(settings.api_keys, k, v)
            if "defaults" in data:
                for k, v in data["defaults"].items():
                    if hasattr(settings.defaults, k):
                        setattr(settings.defaults, k, v)
        return settings

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "floor": {
                "policy": self.floor.policy,
                "max_agents": self.floor.max_agents,
                "timeout_seconds": self.floor.timeout_seconds,
            },
            "api_keys": {k: v for k, v in {
                "anthropic": self.api_keys.anthropic,
                "openai": self.api_keys.openai,
                "google": self.api_keys.google,
                "huggingface": self.api_keys.huggingface,
            }.items() if v is not None},
            "defaults": {
                "llm_model_anthropic": self.defaults.llm_model_anthropic,
                "vision_model_anthropic": self.defaults.vision_model_anthropic,
                "llm_model_openai": self.defaults.llm_model_openai,
                "image_model_openai": self.defaults.image_model_openai,
                "video_model_openai": self.defaults.video_model_openai,
                "vision_model_openai": self.defaults.vision_model_openai,
                "llm_model_google": self.defaults.llm_model_google,
                "image_model_google": self.defaults.image_model_google,
                "vision_model_google": self.defaults.vision_model_google,
                "music_model_google": self.defaults.music_model_google,
                "video_model_google": self.defaults.video_model_google,
                "relevance_filter": self.defaults.relevance_filter,
            },
        }
        with open(CONFIG_FILE, "wb") as f:
            tomli_w.dump(data, f)
