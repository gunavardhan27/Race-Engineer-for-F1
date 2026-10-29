"""Tests for the UsageGuard system, rate limiting, fallbacks, and paid protection."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import time
from typing import AsyncIterator
import unittest.mock as mock

import numpy as np
import pytest

from config import EdgeTTSRateLimits, LLMConfig, ModelRateLimits, UsageConfig
from llm import GroqProvider, LLMClient, LLMProvider
from stt import GroqWhisperSTT, SpeechToText
from tts import EdgeTTS, TTSProvider
from usage import UsageGuard, parse_duration_s


class DummyLLMProvider:
    def __init__(self, reply: str = "Copy, understood.", fail: bool = False):
        self.reply = reply
        self.fail = fail

    async def stream(self, system: str, user: str, max_tokens: int = 60, timeout_s: float = 4.0) -> AsyncIterator[str]:
        if self.fail:
            raise RuntimeError("Provider failed or rate limited")
        for word in self.reply.split():
            yield word + " "

    async def warmup(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class DummyTTSProvider:
    def __init__(self):
        self.called = False

    async def synth(self, text: str) -> tuple[np.ndarray, int]:
        self.called = True
        return np.ones(1600, dtype=np.float32), 16000

    async def warmup(self) -> None:
        pass


class DummySTTProvider:
    def __init__(self, text: str = "box box"):
        self.text = text
        self.called = False

    def transcribe(self, audio: np.ndarray, initial_prompt: str = "") -> str:
        self.called = True
        return self.text


def test_duration_parsing():
    assert parse_duration_s("2m59.56s") == pytest.approx(179.56)
    assert parse_duration_s("6s") == 6.0
    assert parse_duration_s("1.5ms") == 0.0015
    assert parse_duration_s("300ms") == 0.3
    assert parse_duration_s("1h2m3s") == 3723.0
    assert parse_duration_s(15) == 15.0
    assert parse_duration_s(None) == 0.0


def test_token_estimation():
    prompt = "Hello, what is my gap to the car ahead?"  # 39 chars
    est = UsageGuard.estimate_tokens(prompt, max_tokens=60)
    expected = int(39 / 4) + 60  # 9 + 60 = 69
    assert est == expected


def test_minute_request_limit_denial_at_80_percent(tmp_path):
    db_file = tmp_path / "test_storage.db"
    cfg = UsageConfig(
        allow_paid=False,
        max_fraction=0.8,
        groq_models={"llama-3.1-8b-instant": ModelRateLimits(rpm=10, rpd=1000, tpm=6000, tpd=500000)},
    )
    guard = UsageGuard(config=cfg, db_path=str(db_file))

    # At RPM=10, max_fraction=0.8 -> limit is 8 requests/minute
    for _ in range(8):
        allowed, reason = guard.check_allowed("groq", est_tokens=10, model="llama-3.1-8b-instant")
        assert allowed is True
        guard.record_usage("groq", requests=1, tokens=10, model="llama-3.1-8b-instant")

    # 9th request should cross 80% (8) -> Denied
    allowed, reason = guard.check_allowed("groq", est_tokens=10, model="llama-3.1-8b-instant")
    assert allowed is False
    assert "Minute request limit reached" in reason


def test_daily_request_limit_denial_at_80_percent(tmp_path):
    db_file = tmp_path / "test_storage.db"
    cfg = UsageConfig(
        allow_paid=False,
        max_fraction=0.8,
        groq_models={"llama-3.1-8b-instant": ModelRateLimits(rpm=100, rpd=50, tpm=6000, tpd=500000)},
    )
    guard = UsageGuard(config=cfg, db_path=str(db_file))

    # Record 40 requests today (80% of 50 RPD is 40)
    for _ in range(40):
        guard.record_usage("groq", requests=1, tokens=5, model="llama-3.1-8b-instant")

    # 41st request should be denied
    allowed, reason = guard.check_allowed("groq", est_tokens=5, model="llama-3.1-8b-instant")
    assert allowed is False
    assert "Daily request limit reached" in reason


def test_daily_totals_survive_restart(tmp_path):
    db_file = tmp_path / "persist.db"
    cfg = UsageConfig(
        allow_paid=False,
        max_fraction=0.8,
        groq_models={"llama-3.1-8b-instant": ModelRateLimits(rpm=100, rpd=20, tpm=6000, tpd=500000)},
    )

    # First app run: record 16 requests
    guard1 = UsageGuard(config=cfg, db_path=str(db_file))
    for _ in range(16):
        guard1.record_usage("groq", requests=1, tokens=20, model="llama-3.1-8b-instant")

    # App restarts: new UsageGuard instance pointing to same SQLite database
    guard2 = UsageGuard(config=cfg, db_path=str(db_file))
    status = guard2.get_status()
    service_stats = status["services"]["groq"]
    assert service_stats["daily"]["requests"] == 16
    assert service_stats["daily"]["tokens"] == 320

    # 80% of 20 RPD is 16. 17th request must be denied immediately after restart!
    allowed, reason = guard2.check_allowed("groq", est_tokens=10, model="llama-3.1-8b-instant")
    assert allowed is False
    assert "Daily request limit reached" in reason


def test_http_429_with_retry_after_pauses_service(tmp_path):
    db_file = tmp_path / "test_429.db"
    guard = UsageGuard(db_path=str(db_file))

    # Record a 429 with retry-after: 12 seconds
    guard.record_usage(
        "groq",
        status_code=429,
        headers={"retry-after": "12s"},
        model="llama-3.1-8b-instant",
    )

    is_p, rem, reason = guard.is_paused("groq")
    assert is_p is True
    assert 10.0 <= rem <= 12.0

    # check_allowed should deny because service is paused
    allowed, reason = guard.check_allowed("groq")
    assert allowed is False
    assert "paused" in reason.lower()


def test_paid_tier_headers_clamped_when_allow_paid_false(tmp_path, caplog):
    db_file = tmp_path / "test_paid.db"
    cfg = UsageConfig(
        allow_paid=False,
        max_fraction=0.8,
        groq_models={"llama-3.1-8b-instant": ModelRateLimits(rpm=30, rpd=14400, tpm=6000, tpd=500000)},
    )
    guard = UsageGuard(config=cfg, db_path=str(db_file))

    # Account has a paid tier, so Groq returns high limits: RPM 200, TPM 100000
    paid_headers = {
        "x-ratelimit-limit-requests": "200",
        "x-ratelimit-remaining-requests": "195",
        "x-ratelimit-limit-tokens": "100000",
        "x-ratelimit-remaining-tokens": "99000",
    }

    with caplog.at_level("WARNING"):
        guard.record_usage("groq", requests=1, tokens=100, headers=paid_headers, model="llama-3.1-8b-instant")

    # Must log warning about paid tier header
    assert any("exceeds configured free-tier limits" in r.message for r in caplog.records)

    # Live ceiling enforcement: free tier configured RPM is 30.
    # At 80% of 30 = 24 requests, minute limit triggers even though header says 200!
    for _ in range(23):
        guard.record_usage("groq", requests=1, tokens=5, model="llama-3.1-8b-instant")

    allowed, reason = guard.check_allowed("groq", est_tokens=10, model="llama-3.1-8b-instant")
    assert allowed is False
    assert "Minute request limit reached" in reason


def test_fallback_chain_llm(tmp_path):
    async def _run():
        db_file = tmp_path / "test_llm_fallback.db"
        cfg_usage = UsageConfig(
            allow_paid=False,
            max_fraction=0.8,
            groq_models={"llama-3.1-8b-instant": ModelRateLimits(rpm=1, rpd=10, tpm=6000, tpd=500000)},
        )
        guard = UsageGuard(config=cfg_usage, db_path=str(db_file))

        llm_cfg = LLMConfig(primary="groq", fallback="ollama")
        client = LLMClient(config=llm_cfg, usage_guard=guard)

        client.primary = GroqProvider(model="llama-3.1-8b-instant", usage_guard=guard)
        client.fallback = DummyLLMProvider(reply="Local fallback answering radio check.")

        tokens = []
        async for tok in client.stream("system", "user"):
            tokens.append(tok)

        reply = "".join(tokens).strip()
        assert "Local fallback answering radio check." in reply
        assert client.last_provider_used == "ollama"
        assert guard.last_provider_used == "ollama"

    asyncio.run(_run())


def test_tts_cache_hit_bypasses_cloud_usage(tmp_path):
    async def _run():
        cache_dir = tmp_path / "audio_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        db_file = tmp_path / "test_tts.db"

        guard = UsageGuard(db_path=str(db_file))
        fallback = DummyTTSProvider()
        tts = EdgeTTS(voice="en-GB-RyanNeural", usage_guard=guard, fallback_engine=fallback, cache_dir=str(cache_dir))

        import hashlib
        key = hashlib.sha256("en-GB-RyanNeural:Box now, box now".encode("utf-8")).hexdigest()
        cache_file = cache_dir / f"{key}.npz"
        np.savez_compressed(cache_file, audio=np.zeros(8000, dtype=np.float32), sample_rate=16000)

        audio, sr = await tts.synth("Box now, box now")
        assert len(audio) == 8000
        assert sr == 16000

        status = guard.get_status()
        tts_stats = status["services"]["edge_tts"]
        assert tts_stats["minute"]["requests"] == 0
        assert tts_stats["daily"]["characters"] == 0

    asyncio.run(_run())


def test_tts_denial_falls_back_to_piper(tmp_path):
    async def _run():
        cache_dir = tmp_path / "audio_cache_empty"
        cache_dir.mkdir(parents=True, exist_ok=True)
        db_file = tmp_path / "test_tts_fallback.db"

        cfg_usage = UsageConfig(
            allow_paid=False,
            max_fraction=0.8,
            edge_tts=EdgeTTSRateLimits(rpm=1, rpd=10, chars_per_min=10, chars_per_day=100),
        )
        guard = UsageGuard(config=cfg_usage, db_path=str(db_file))
        fallback = DummyTTSProvider()
        tts = EdgeTTS(voice="en-GB-RyanNeural", usage_guard=guard, fallback_engine=fallback, cache_dir=str(cache_dir))

        long_text = "This text is too long and should exceed the 8 chars limit!"
        audio, sr = await tts.synth(long_text)

        assert fallback.called is True
        assert len(audio) == 1600

        status = guard.get_status()
        assert status["services"]["edge_tts"]["minute"]["requests"] == 0

    asyncio.run(_run())


def test_stt_denial_falls_back_to_local_whisper(tmp_path):
    db_file = tmp_path / "test_stt_fallback.db"
    cfg_usage = UsageConfig(
        allow_paid=False,
        max_fraction=0.8,
        groq_models={"whisper-large-v3": ModelRateLimits(rpm=1, rpd=10, ash=10, asd=50)},
    )
    guard = UsageGuard(config=cfg_usage, db_path=str(db_file))
    fallback = DummySTTProvider(text="faster-whisper local transcription")

    stt = GroqWhisperSTT(
        model="whisper-large-v3",
        api_key="fake-key",
        usage_guard=guard,
        fallback_engine=fallback,
    )

    # Send 45 seconds of audio (exceeds 80% of 50s daily limit)
    fake_audio = np.zeros(16000 * 45, dtype=np.float32)
    text = stt.transcribe(fake_audio)

    assert fallback.called is True
    assert text == "faster-whisper local transcription"


def test_threshold_alerts_logging_50_and_80(tmp_path, caplog):
    db_file = tmp_path / "test_alerts.db"
    cfg = UsageConfig(
        allow_paid=False,
        max_fraction=0.8,
        groq_models={"llama-3.1-8b-instant": ModelRateLimits(rpm=100, rpd=100, tpm=10000, tpd=10000)},
    )
    guard = UsageGuard(config=cfg, db_path=str(db_file))

    with caplog.at_level("WARNING"):
        # Reach 50%
        guard.record_usage("groq", requests=50, tokens=5000, model="llama-3.1-8b-instant")
        assert any("reached 50% of a daily limit" in r.message for r in caplog.records)

        # Reach 80%
        guard.record_usage("groq", requests=30, tokens=3000, model="llama-3.1-8b-instant")
        assert any("reached 80% of a daily limit" in r.message for r in caplog.records)


def test_dashboard_usage_api(tmp_path):
    from fastapi.testclient import TestClient
    from dashboard.server import Broadcaster, create_app
    from f1telemetry.state import RaceState

    db_file = tmp_path / "test_api_usage.db"
    guard = UsageGuard(db_path=str(db_file))
    guard.record_usage("groq", requests=5, tokens=250, model="llama-3.1-8b-instant")
    guard.last_provider_used = "groq"

    state = RaceState()
    broadcaster = Broadcaster()
    app = create_app(state, broadcaster, token="secret-token", usage_guard=guard)
    client = TestClient(app)

    # Unauthorized without token
    resp = client.get("/api/usage")
    assert resp.status_code == 403

    # Authorized with token
    resp = client.get("/api/usage?token=secret-token")
    assert resp.status_code == 200
    data = resp.json()
    assert data["last_provider"] == "groq"
    assert "groq" in data["services"]
    assert data["services"]["groq"]["daily"]["requests"] == 5
    assert data["services"]["groq"]["daily"]["tokens"] == 250


def test_no_spoken_warnings_on_threshold_alert(tmp_path):
    from f1telemetry.deterministic import RadioQueue

    radio = RadioQueue()
    db_file = tmp_path / "test_no_spoken.db"
    cfg = UsageConfig(
        allow_paid=False,
        max_fraction=0.8,
        groq_models={"llama-3.1-8b-instant": ModelRateLimits(rpm=100, rpd=100, tpm=10000, tpd=10000)},
    )
    guard = UsageGuard(config=cfg, db_path=str(db_file))

    # Trigger 50% and 80% threshold warnings
    guard.record_usage("groq", requests=50, tokens=5000, model="llama-3.1-8b-instant")
    guard.record_usage("groq", requests=30, tokens=3000, model="llama-3.1-8b-instant")

    # RadioQueue must remain completely untouched: no spoken callout queued
    assert radio.pop() is None


def test_daily_history_retrieval(tmp_path):
    import sqlite3
    db_file = tmp_path / "test_history.db"
    cfg = UsageConfig(
        allow_paid=False,
        max_fraction=0.8,
        groq_models={"llama-3.1-8b-instant": ModelRateLimits(rpm=30, rpd=1000, tpm=6000, tpd=100000)},
    )
    guard = UsageGuard(config=cfg, db_path=str(db_file))

    # Insert historical data for 2 different days
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO usage_daily (date, service, requests, tokens, audio_seconds, characters) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-10-27", "groq", 100, 20000, 0.0, 0),
        )
        conn.execute(
            "INSERT INTO usage_daily (date, service, requests, tokens, audio_seconds, characters) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-10-28", "edge_tts", 50, 0, 0.0, 15000),
        )
        conn.commit()

    # Query last 7 days
    history = guard.get_daily_history(days=7)
    assert len(history) == 2
    dates = [h["date"] for h in history]
    assert "2026-10-28" in dates
    assert "2026-10-27" in dates

    groq_entry = next(h for h in history if h["service"] == "groq")
    assert groq_entry["requests"] == 100
    assert groq_entry["tokens"] == 20000
    assert groq_entry["max_pct"] == pytest.approx(20.0)  # 20k / 100k TPD = 20%


def test_preflight_check(tmp_path):
    db_file = tmp_path / "test_preflight.db"
    cfg = UsageConfig(
        allow_paid=False,
        max_fraction=0.8,
        groq_models={"llama-3.1-8b-instant": ModelRateLimits(rpm=30, rpd=100, tpm=6000, tpd=1000)},
    )
    guard = UsageGuard(config=cfg, db_path=str(db_file))

    # Clean pre-flight: OK
    pf = guard.run_preflight_check(print_banner=False)
    assert pf["health"] == "OK"
    assert pf["overall_pct"] == 0.0
    assert pf["cutoff_pct"] == 80.0

    # Near limit (60%): NEAR_LIMIT
    guard.record_usage("groq", requests=65, tokens=650, model="llama-3.1-8b-instant")
    pf = guard.run_preflight_check(print_banner=False)
    assert pf["health"] == "NEAR_LIMIT"
    assert pf["overall_pct"] == 65.0

    # Past safety margin (85%): CUTOFF
    guard.record_usage("groq", requests=20, tokens=200, model="llama-3.1-8b-instant")
    pf = guard.run_preflight_check(print_banner=False)
    assert pf["health"] == "CUTOFF"
    assert pf["overall_pct"] >= 80.0


def test_dashboard_usage_history_api(tmp_path):
    from fastapi.testclient import TestClient
    from dashboard.server import Broadcaster, create_app
    from f1telemetry.state import RaceState

    db_file = tmp_path / "test_api_history.db"
    guard = UsageGuard(db_path=str(db_file))
    guard.record_usage("groq", requests=10, tokens=500, model="llama-3.1-8b-instant")

    state = RaceState()
    broadcaster = Broadcaster()
    app = create_app(state, broadcaster, token="history-token", usage_guard=guard)
    client = TestClient(app)

    # 403 without token
    resp = client.get("/api/usage/history")
    assert resp.status_code == 403

    # 200 with token
    resp = client.get("/api/usage/history?days=14&token=history-token")
    assert resp.status_code == 200
    data = resp.json()
    assert data["days"] == 14
    assert len(data["history"]) >= 1
    assert data["history"][0]["service"] == "groq"
    assert "preflight" in data
    assert data["preflight"]["health"] == "OK"


