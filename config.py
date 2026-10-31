"""Configuration loader and schema for the race engineer.

Loads config.yaml into strongly typed dataclasses with CPU-first defaults.
Unknown keys emit warnings rather than raising errors.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("engineer.config")


def load_env_file(dotenv_path: Path | str = ".env") -> None:
    """Loads key-value pairs from a .env file into os.environ if not already set."""
    p = Path(dotenv_path)
    if not p.is_file():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(p, override=False)
        return
    except ImportError:
        pass

    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as e:
        log.warning("Could not load .env file '%s': %s", p, e)


# Automatically load environment variables when config is imported
load_env_file()


@dataclass
class TelemetryConfig:
    host: str = "0.0.0.0"
    port: int = 20777
    ptt_action: int = 1
    ask_port: bool = False


@dataclass
class VolumesConfig:
    engineer: float = 1.0        # Volume for engineer voice (0.0 to 1.0)
    spotter: float = 1.0         # Volume for spotter callouts (0.0 to 1.0)
    menu_beeps: float = 1.0      # Volume for menu and button beeps (0.0 to 1.0)


@dataclass
class PaceConfig:
    slower_lap_threshold_s: float = 0.5   # Gap vs previous valid lap before calling driver slower
    fuel_effect_s_per_kg: float = 0.03    # Expected lap time improvement in seconds per kg of fuel burned
    expectation_margin: int = 3           # Places delta vs expected position before calling above/below expectation


@dataclass
class QualiConfig:
    eliminated_per_segment: int | None = None  # None = derive from car count (e.g. 6 for 22 cars, 5 for 20 cars)
    large_gap_s: float = 1.0                   # Gap to pole in seconds before triggering quali_gap_large


@dataclass
class RadioCheckConfig:
    on_session_start: bool = True # Automatic check 3s after session start


@dataclass
class SoundTestConfig:
    on_app_start: bool = True     # Play "Engineer Online, Radio check radio check." on app launch


@dataclass
class AudioConfig:
    input_device: str | int | None = None
    output_device: str | int | None = None
    sample_rate: int = 16000
    volumes: VolumesConfig = field(default_factory=VolumesConfig)


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
    model: str = "qwen/qwen3.8-27b"
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
    detectors: bool = False


@dataclass
class OvertakeConfig:
    confirm_time_s: float = 2.0
    confirm_distance_m: float = 80.0
    proximity_m: float = 60.0
    clean_braking_delta_max_m: float = 10.0
    brave_braking_delta_min_m: float = 10.0
    brave_braking_delta_max_m: float = 25.0
    brave_start_gap_min_m: float = -10.0
    brave_start_gap_max_m: float = -5.0
    divebomb_start_gap_max_m: float = -10.0
    divebomb_braking_delta_min_m: float = 25.0
    divebomb_min_speed_delta_kph: float = 15.0
    divebomb_exit_speed_delta_kph: float = -10.0
    contact_window_s: float = 2.0
    forced_off_speed_drop_kph: float = 25.0
    ran_deep_min_speed_delta_kph: float = 15.0
    ran_deep_exit_speed_delta_kph: float = 10.0


@dataclass
class FinishConfig:
    photo_finish_margin_s: float = 0.5
    close_margin_s: float = 3.0
    comfortable_margin_s: float = 10.0
    podium_held_behind_margin_s: float = 1.0
    podium_missed_higher_ahead_margin_s: float = 1.0
    missed_podium_ahead_margin_s: float = 2.0


@dataclass
class DefendConfig:
    zone_distance_min_m: float = 150.0
    zone_distance_max_m: float = 300.0
    close_distance_m: float = 15.0
    close_time_s: float = 0.4
    inside_move_threshold_m: float = 1.0
    inside_distance_threshold_m: float = 10.0
    cooldown_overall_s: float = 20.0


@dataclass
class ModelRateLimits:
    rpm: int = 30
    rpd: int = 14400
    tpm: int = 6000
    tpd: int = 500000
    ash: int = 7200      # Audio seconds per hour (Whisper)
    asd: int = 28800     # Audio seconds per day (Whisper)


@dataclass
class EdgeTTSRateLimits:
    # Note: self-imposed conservative limit, not an official quota
    rpm: int = 20
    rpd: int = 2000
    chars_per_min: int = 10000
    chars_per_day: int = 200000


@dataclass
class UsageConfig:
    allow_paid: bool = False
    max_fraction: float = 0.8
    groq_models: dict[str, ModelRateLimits] = field(default_factory=lambda: {
        "llama-3.1-8b-instant": ModelRateLimits(rpm=30, rpd=14400, tpm=6000, tpd=500000),
        "llama-3.3-70b-versatile": ModelRateLimits(rpm=30, rpd=1000, tpm=12000, tpd=100000),
        "whisper-large-v3": ModelRateLimits(rpm=20, rpd=2000, ash=7200, asd=28800),
        "whisper-large-v3-turbo": ModelRateLimits(rpm=20, rpd=2000, ash=7200, asd=28800),
    })
    edge_tts: EdgeTTSRateLimits = field(default_factory=EdgeTTSRateLimits)

    def __post_init__(self):
        if isinstance(self.groq_models, dict):
            new_models = {}
            for k, v in self.groq_models.items():
                if isinstance(v, dict):
                    new_models[k] = ModelRateLimits(**v)
                elif isinstance(v, ModelRateLimits):
                    new_models[k] = v
            self.groq_models = new_models
        if isinstance(self.edge_tts, dict):
            self.edge_tts = EdgeTTSRateLimits(**self.edge_tts)


@dataclass
class CacheConfig:
    audio_max_mb: float = 100.0


@dataclass
class AppConfig:
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    spotter: SpotterConfig = field(default_factory=SpotterConfig)
    debrief: DebriefConfig = field(default_factory=DebriefConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    usage: UsageConfig = field(default_factory=UsageConfig)
    radio_check: RadioCheckConfig = field(default_factory=RadioCheckConfig)
    sound_test: SoundTestConfig = field(default_factory=SoundTestConfig)
    volumes: VolumesConfig = field(default_factory=VolumesConfig)
    pace: PaceConfig = field(default_factory=PaceConfig)
    quali: QualiConfig = field(default_factory=QualiConfig)
    overtake: OvertakeConfig = field(default_factory=OvertakeConfig)
    finish: FinishConfig = field(default_factory=FinishConfig)
    defend: DefendConfig = field(default_factory=DefendConfig)


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
