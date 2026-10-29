"""Unit tests for configuration loading and merging."""
import logging
from pathlib import Path
import tempfile
import pytest

from config import AppConfig, load_config
from main import parse_args


def test_default_config():
    """AppConfig defaults should have sensible values and CPU-first settings."""
    cfg = AppConfig()
    assert cfg.telemetry.host == "127.0.0.1"
    assert cfg.telemetry.port == 20777
    assert cfg.telemetry.ptt_action == 1
    assert cfg.stt.device == "cpu"
    assert cfg.stt.compute_type == "int8"
    assert cfg.tts.device == "cpu"
    assert cfg.llm.ollama.num_gpu == 0
    assert cfg.llm.primary == "groq"
    assert cfg.llm.fallback == "ollama"


def test_load_nonexistent_config_falls_back_to_defaults(caplog):
    """Loading a non-existent file path should log info/warning and return defaults without crashing."""
    with caplog.at_level(logging.INFO):
        cfg = load_config("nonexistent_path_to_config.yaml")
    assert isinstance(cfg, AppConfig)
    assert cfg.telemetry.port == 20777


def test_load_config_yaml():
    """Loading config.yaml in workspace should match expected schema."""
    cfg = load_config("config.yaml")
    assert cfg.telemetry.host == "127.0.0.1"
    assert cfg.telemetry.port == 20777
    assert cfg.stt.model == "small.en"
    assert cfg.tts.engine == "piper"
    assert cfg.llm.groq.model == "llama-3.1-8b-instant"


def test_unknown_keys_emit_warning_not_crash(caplog):
    """Unknown keys in config YAML should produce a warning and still parse known keys."""
    yaml_content = """
telemetry:
  host: "192.168.1.50"
  port: 20778
  unknown_telemetry_key: 12345
unknown_root_section:
  hello: "world"
stt:
  device: "cpu"
  unexpected_field: true
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f_path = f.name

    try:
        with caplog.at_level(logging.WARNING):
            cfg = load_config(f_path)

        assert cfg.telemetry.host == "192.168.1.50"
        assert cfg.telemetry.port == 20778
        assert cfg.stt.device == "cpu"

        # Verify warnings were logged for unknown keys
        warning_messages = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
        assert any("unknown_telemetry_key" in m for m in warning_messages)
        assert any("unknown_root_section" in m for m in warning_messages)
        assert any("unexpected_field" in m for m in warning_messages)
    finally:
        Path(f_path).unlink(missing_ok=True)


def test_cli_flags_override_config():
    """CLI flags must override values loaded from config."""
    cfg = load_config("config.yaml")

    args = parse_args(["--host", "10.0.0.1", "--port", "20888", "--ptt-action", "3", "--redis", "redis://custom:6379/1", "--redis-hz", "20"])

    host = args.host if args.host is not None else cfg.telemetry.host
    port = args.port if args.port is not None else cfg.telemetry.port
    ptt_action = args.ptt_action if args.ptt_action is not None else cfg.telemetry.ptt_action
    redis_url = args.redis if args.redis is not None else (cfg.redis.url if cfg.redis.enabled else None)
    redis_hz = args.redis_hz if args.redis_hz is not None else cfg.redis.hz

    assert host == "10.0.0.1"
    assert port == 20888
    assert ptt_action == 3
    assert redis_url == "redis://custom:6379/1"
    assert redis_hz == 20.0
