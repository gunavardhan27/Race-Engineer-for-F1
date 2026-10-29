"""Configuration loader and schema for the race engineer.

Loads config.yaml into strongly typed dataclasses with CPU-first defaults.
Unknown keys emit warnings rather than raising errors.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("engineer.config")


@dataclass
class TelemetryConfig:
    host: str = "127.0.0.1"
    port: int = 20777
    ptt_action: int = 1


@dataclass
class AudioConfig:
    input_device: str | int | None = None
    output_device: str | int | None = None
    sample_rate: int = 16000


@dataclass
class STTConfig:
    model: str = "small.en"
    compute_type: str = "int8"
    device: str = "cpu"
    beam_size: int = 1
    min_duration_s: float = 0.25
    max_duration_s: float = 15.0
    pre_roll_ms: int = 300
    ptt_source: str = "udp"       # "udp" or "joystick"
    joystick_index: int = 0
    joystick_button: int = 0


@dataclass
class TTSConfig:
    engine: str = "piper"         # "piper", "kokoro", "edge"
    voice: str = "en_GB-alan-medium"
    device: str = "cpu"
    radio_fx: bool = True


@dataclass
class GroqConfig:
    model: str = "llama-3.1-8b-instant"
    api_key_env: str = "GROQ_API_KEY"


@dataclass
class OllamaConfig:
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:3b-instruct"
    num_gpu: int = 0              # 0 = CPU only


@dataclass
class OpenAICompatibleConfig:
    base_url: str = "http://localhost:8080/v1"
    model: str = "local"


@dataclass
class LLMConfig:
    primary: str = "groq"
    fallback: str | None = "ollama"
    timeout_s: float = 4.0
    max_tokens: int = 60
    groq: GroqConfig = field(default_factory=GroqConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    openai_compatible: OpenAICompatibleConfig = field(default_factory=OpenAICompatibleConfig)


@dataclass
class KnowledgeConfig:
    enabled: bool = True
    tracks_dir: str = "knowledge/tracks"
    general_path: str = "knowledge/general.md"
    max_tokens: int = 400


@dataclass
class RedisConfig:
    enabled: bool = False
    url: str = "redis://localhost:6379/0"
    hz: float = 10.0


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8765
    hz: float = 10.0
    token: str | None = None


@dataclass
class SpotterConfig:
    enabled: bool = True
    lateral_distance_m: float = 3.6
    longitudinal_distance_m: float = 4.8
    still_there_interval_s: float = 2.5
    min_speed_kph: float = 20.0
    ttl_s: float = 0.6


@dataclass
class DebriefConfig:
    auto_lap_debrief: bool = True
    reference_lap: str = "session"   # "session", "stint", "all_time"
    worst_corner_threshold_s: float = 0.15
    track_limits_callouts: bool = True
    track_limits_penalty_at: int | None = None


@dataclass
class PersonaConfig:
    name: str = "cocky"                   # file in lines/ and prompts/personas/; "professional" = no reactions
    profanity: str = "mild"               # "none" | "mild" | "strong"
    reaction_frequency: str = "normal"    # "low" | "normal" | "high"
    min_gap_s: float = 20.0               # minimum time between personality reactions
    spin_angle_deg: float = 60.0
    spin_entry_speed_kph: float = 50.0
    spin_exit_speed_kph: float = 30.0
    spin_window_s: float = 3.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"


@dataclass
class AppConfig:
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    spotter: SpotterConfig = field(default_factory=SpotterConfig)
    debrief: DebriefConfig = field(default_factory=DebriefConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _load_dataclass(cls: type, data: Any, prefix: str = "") -> Any:
    """Recursively instantiates a dataclass from a dict, logging warnings on unknown keys."""
    if not isinstance(data, dict):
        if data is not None:
            log.warning("Expected section dict at '%s', got %s; using defaults", prefix, type(data).__name__)
        return cls()

    import typing
    try:
        type_hints = typing.get_type_hints(cls)
    except Exception:
        type_hints = {}

    field_map = {f.name: f for f in fields(cls)}
    kwargs = {}

    for key, val in data.items():
        field_path = f"{prefix}.{key}" if prefix else str(key)
        if key not in field_map:
            log.warning("Unknown configuration key: '%s'", field_path)
            continue

        f_info = field_map[key]
        f_type = type_hints.get(key, f_info.type)

        # Handle nested dataclass instances
        if isinstance(f_type, type) and is_dataclass(f_type):
            kwargs[key] = _load_dataclass(f_type, val, prefix=field_path)
        elif callable(f_info.default_factory) and is_dataclass(f_info.default_factory):
            kwargs[key] = _load_dataclass(f_info.default_factory, val, prefix=field_path)
        else:
            # Handle standard field or direct value
            kwargs[key] = val

    return cls(**kwargs)



def load_config(path: str | Path | None = "config.yaml") -> AppConfig:
    """Loads configuration from YAML file, falling back to sensible defaults on error or missing file."""
    if path is None:
        log.info("No config path provided; using default configuration")
        return AppConfig()

    p = Path(path)
    if not p.is_file():
        log.info("Config file '%s' not found; using default configuration", p)
        return AppConfig()

    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not raw:
            log.warning("Config file '%s' is empty; using default configuration", p)
            return AppConfig()
        return _load_dataclass(AppConfig, raw)
    except Exception as e:
        log.warning("Failed to parse config file '%s': %s; using defaults", p, e)
        return AppConfig()
