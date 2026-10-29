"""Unit tests for the latency tracker."""
import logging
import pytest

from latency import LatencyTracker


def test_single_full_transmission_durations(caplog):
    """Verifies that stage durations are calculated correctly in milliseconds."""
    tracker = LatencyTracker()

    with caplog.at_level(logging.INFO):
        t_id = tracker.start_transmission(timestamp=100.0)
        assert t_id == 1

        tracker.record("stt_done", timestamp=100.150)         # STT = 150 ms
        tracker.record("route_done", timestamp=100.152)       # Route = 2 ms
        tracker.record("llm_first_token", timestamp=100.352)  # LLM 1st = 200 ms
        tracker.record("llm_done", timestamp=100.552)         # LLM total = 400 ms
        tracker.record("tts_first_audio", timestamp=100.502)  # TTS 1st = 150 ms (from llm_first_token)
        tracker.record("playback_done", timestamp=101.402)    # Playback = 900 ms, Total = 1402 ms

    # Check recorded durations
    completed = tracker._completed_transmissions
    assert len(completed) == 1
    t = completed[0]
    assert pytest.approx(t.durations_ms["stt"], 0.1) == 150.0
    assert pytest.approx(t.durations_ms["route"], 0.1) == 2.0
    assert pytest.approx(t.durations_ms["llm_1st"], 0.1) == 200.0
    assert pytest.approx(t.durations_ms["llm_done"], 0.1) == 400.0
    assert pytest.approx(t.durations_ms["tts_1st"], 0.1) == 150.0
    assert pytest.approx(t.durations_ms["playback"], 0.1) == 900.0
    assert pytest.approx(t.durations_ms["total"], 0.1) == 1402.0

    # Check rolling average
    assert pytest.approx(tracker.rolling_average("total"), 0.1) == 1402.0
    assert pytest.approx(tracker.rolling_average("stt"), 0.1) == 150.0

    # Check log message was emitted
    log_text = caplog.text
    assert "[LATENCY] #1" in log_text
    assert "stt=150.0ms" in log_text
    assert "total=1402.0ms" in log_text


def test_deterministic_transmission_without_llm():
    """Deterministic questions route without LLM stages; TTS start reference is route_done."""
    tracker = LatencyTracker()

    tracker.start_transmission(timestamp=10.0)
    tracker.record("stt_done", timestamp=10.100)        # 100 ms
    tracker.record("route_done", timestamp=10.101)      # 1 ms
    tracker.record("tts_first_audio", timestamp=10.201) # 100 ms from route_done
    tracker.record("playback_done", timestamp=10.801)   # 600 ms

    t = tracker._completed_transmissions[0]
    assert pytest.approx(t.durations_ms["stt"], 0.1) == 100.0
    assert pytest.approx(t.durations_ms["route"], 0.1) == 1.0
    assert pytest.approx(t.durations_ms["tts_1st"], 0.1) == 100.0
    assert pytest.approx(t.durations_ms["playback"], 0.1) == 600.0
    assert pytest.approx(t.durations_ms["total"], 0.1) == 801.0
    assert "llm_1st" not in t.durations_ms


def test_rolling_averages_over_multiple_transmissions():
    """Rolling average should compute correctly across multiple transmissions."""
    tracker = LatencyTracker(window_size=3)

    # Transmission 1: total = 1000ms
    tracker.start_transmission(timestamp=0.0)
    tracker.record("playback_done", timestamp=1.0)

    # Transmission 2: total = 2000ms
    tracker.start_transmission(timestamp=10.0)
    tracker.record("playback_done", timestamp=12.0)

    assert pytest.approx(tracker.rolling_average("total"), 0.1) == 1500.0

    # Transmission 3: total = 3000ms (avg of 1000, 2000, 3000 = 2000)
    tracker.start_transmission(timestamp=20.0)
    tracker.record("playback_done", timestamp=23.0)

    assert pytest.approx(tracker.rolling_average("total"), 0.1) == 2000.0

    # Transmission 4: total = 4000ms (window size 3: avg of 2000, 3000, 4000 = 3000)
    tracker.start_transmission(timestamp=30.0)
    tracker.record("playback_done", timestamp=34.0)

    assert pytest.approx(tracker.rolling_average("total"), 0.1) == 3000.0


def test_cancel_active():
    """Canceling active transmission drops it without skewing averages."""
    tracker = LatencyTracker()

    tracker.start_transmission(timestamp=0.0)
    tracker.cancel_active()

    assert tracker.rolling_average("total") is None
    assert len(tracker._completed_transmissions) == 0


def test_reset():
    """Reset clears history and counter."""
    tracker = LatencyTracker()

    tracker.start_transmission(timestamp=0.0)
    tracker.record("playback_done", timestamp=1.0)
    assert len(tracker._completed_transmissions) == 1

    tracker.reset()
    assert len(tracker._completed_transmissions) == 0
    assert tracker.rolling_average("total") is None
