"""Unit tests for audio capture: PTT start/stop, pre-roll buffer, min duration, and max cap."""
import numpy as np
import pytest

from audio_in import AudioCapture


def test_ptt_start_stop_with_pre_roll():
    """Verify that recorded audio includes the ~300 ms pre-roll and new audio chunks."""
    collected = []

    cap = AudioCapture(
        sample_rate=16000,
        pre_roll_ms=300,        # 4800 samples
        min_duration_s=0.25,    # 4000 samples
        max_duration_s=15.0,
        on_audio_recorded=lambda a: collected.append(a),
        start_stream=False,
    )

    # 1. Feed 300 ms of pre-roll audio (filled with 0.5f)
    pre_roll_chunk = np.full(4800, 0.5, dtype=np.float32)
    cap.feed_audio(pre_roll_chunk)

    # 2. PTT pressed
    cap.on_ptt(True)

    # 3. Feed 500 ms of live audio (8000 samples filled with 0.9f)
    live_chunk = np.full(8000, 0.9, dtype=np.float32)
    cap.feed_audio(live_chunk)

    # 4. PTT released
    cap.on_ptt(False)

    assert len(collected) == 1
    result = collected[0]
    # Total samples should be 4800 (pre-roll) + 8000 (live) = 12800 samples (0.8s)
    assert len(result) == 12800
    # First 4800 samples must match the pre-roll data
    np.testing.assert_allclose(result[:4800], 0.5)
    # Remaining 8000 samples must match live speech
    np.testing.assert_allclose(result[4800:], 0.9)


def test_min_duration_discard():
    """Presses shorter than 250 ms must be ignored and not handed to STT."""
    collected = []

    cap = AudioCapture(
        sample_rate=16000,
        pre_roll_ms=0,          # 0 pre-roll for pure duration test
        min_duration_s=0.25,    # 4000 samples
        max_duration_s=15.0,
        on_audio_recorded=lambda a: collected.append(a),
        start_stream=False,
    )

    cap.on_ptt(True)
    # Feed 100 ms of audio (1600 samples < 4000 min_samples)
    short_chunk = np.full(1600, 0.2, dtype=np.float32)
    cap.feed_audio(short_chunk)
    cap.on_ptt(False)

    # Should be discarded because 100 ms < 250 ms threshold
    assert len(collected) == 0


def test_max_duration_cap():
    """Transmissions must be capped at 15.0 seconds (240,000 samples at 16 kHz)."""
    collected = []

    cap = AudioCapture(
        sample_rate=16000,
        pre_roll_ms=0,
        min_duration_s=0.25,
        max_duration_s=15.0,    # 240,000 samples
        on_audio_recorded=lambda a: collected.append(a),
        start_stream=False,
    )

    cap.on_ptt(True)
    # Feed 16 seconds of audio in 1-second chunks
    one_sec = np.full(16000, 0.1, dtype=np.float32)
    for _ in range(16):
        cap.feed_audio(one_sec)

    assert len(collected) == 1
    result = collected[0]
    # Length must be exactly capped at 15 seconds
    assert len(result) == 240000


def test_empty_or_instant_release():
    """Instant release without audio should cleanly discard without errors."""
    collected = []

    cap = AudioCapture(
        sample_rate=16000,
        pre_roll_ms=300,
        min_duration_s=0.25,
        on_audio_recorded=lambda a: collected.append(a),
        start_stream=False,
    )

    cap.on_ptt(True)
    cap.on_ptt(False)
    assert len(collected) == 0
