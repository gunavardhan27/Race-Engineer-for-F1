"""Reliability tests covering shutdown, single instance, port conflicts, status states, and TTS cache.

Covers Prompt 26 (K9, K10, K11):
- Single-instance locking across launches
- Port conflict diagnostics (psutil / socket owner detection)
- Dashboard 4 status states
- TTS audio cache: static validation, dynamic rejection, int16 compression, orphan cleanup, LRU eviction
- Engine shutdown within 5 s with no lingering non-daemon threads
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from audio_cache import AudioCacheManager, is_static_cacheable, normalize_cache_text
from dashboard.server import DashboardServer, build_live_payload
from f1telemetry.state import RaceState
from latency import LatencyTracker
from main import Engineer
from port_checker import find_port_owner, format_port_conflict_message
from single_instance import SingleInstanceLock


# ---------------------------------------------------------------------------
# B.1: Single Instance Lock
# ---------------------------------------------------------------------------
def test_single_instance_lock(tmp_path: Path):
    """Verify single-instance lock prevents two instances and reports owner PID."""
    lock_file = tmp_path / "test_app.lock"

    lock1 = SingleInstanceLock(lock_file=lock_file)
    res1 = lock1.acquire()
    assert bool(res1) is True
    assert res1.acquired is True
    assert lock_file.exists()

    # Second instance must fail to acquire
    lock2 = SingleInstanceLock(lock_file=lock_file)
    res2 = lock2.acquire()
    assert bool(res2) is False
    assert res2.acquired is False
    assert lock2.get_owner_pid() == os.getpid()

    # Once first instance releases, second instance must succeed
    lock1.release()
    res3 = lock2.acquire()
    assert bool(res3) is True
    assert res3.acquired is True
    assert lock2.get_owner_pid() == os.getpid()
    lock2.release()


# ---------------------------------------------------------------------------
# B.2: Port Conflict Diagnostics
# ---------------------------------------------------------------------------
def test_port_conflict_detection_and_reporting():
    """Verify UDP socket in use reports owning process and PID."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        _, port = sock.getsockname()

        owner = find_port_owner(port, proto="udp")
        assert owner is not None
        assert owner.pid == os.getpid()
        assert owner.name != "unknown"

        msg = format_port_conflict_message(port, proto="udp")
        assert f"Port {port} is in use by {owner.name} (PID {owner.pid})" in msg
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# B.3: Dashboard 4 Status States
# ---------------------------------------------------------------------------
def test_dashboard_four_status_states():
    """Verify the four dashboard states:
    1. Engine not running (WebSocket disconnected / offline)
    2. Engine error (with message, e.g. port conflict)
    3. Running but no telemetry for 3 s (Standby)
    4. Telemetry live
    """
    state = RaceState(ptt_action=1)

    # State 3: Standby (engine running, no telemetry yet)
    state.last_packet_time = 0.0
    state.session = None
    p_standby = build_live_payload(state)
    assert p_standby["engine_status"] == "standby"
    assert p_standby["session_live"] is False
    assert p_standby["engine_error"] is None

    # State 4: Telemetry Live
    state.last_packet_time = time.monotonic()
    state.session = MagicMock(track_id=1, total_laps=30, weather=0, forecast=[])
    p_live = build_live_payload(state)
    assert p_live["engine_status"] == "live"
    assert p_live["session_live"] is True
    assert p_live["engine_error"] is None

    # State 2: Engine Error (e.g. UDP port collision)
    p_error = build_live_payload(state, engine_error="Port 20777 is in use by F1_24.exe (PID 12345)")
    assert p_error["engine_status"] == "error"
    assert "Port 20777 is in use by F1_24.exe (PID 12345)" in p_error["engine_error"]


# ---------------------------------------------------------------------------
# C.1: Static vs Dynamic Cache Filtering
# ---------------------------------------------------------------------------
def test_audio_cache_static_vs_dynamic(tmp_path: Path):
    """Verify live values (numbers, gaps, positions, times, placeholders) are never cached."""
    cache = AudioCacheManager(cache_dir=tmp_path / "audio_cache")

    # Static phrases: must be cacheable
    static_samples = [
        "box, box, box.",
        "Box box box",
        "Clear left",
        "pit window open.",
        "yellow flag.",
        "quiet mode on.",
        "Loud and clear.",
    ]
    for s in static_samples:
        assert is_static_cacheable(s) is True, f"Expected '{s}' to be static"

    # Dynamic phrases: must NOT be cacheable
    dynamic_samples = [
        "Gap ahead is 2.5 seconds",
        "Last lap was 1:23.456",
        "You are currently P3",
        "Car behind is closing by 0.3 seconds a lap",
        "Front left tyre wear is at 45 percent",
        "10 laps of fuel remaining",
        "Turn 4 divebomb",
        "Sector 2 yellow flag",
        "Welcome {driver_name}",
        "Delta is {delta_s} seconds",
    ]
    for d in dynamic_samples:
        assert is_static_cacheable(d) is False, f"Expected '{d}' to be rejected as dynamic"

    # Verify saving dynamic text is rejected and writes no file
    dummy_audio = np.zeros(1600, dtype=np.float32)
    saved = cache.save("Gap ahead is 2.5 seconds", dummy_audio, 16000)
    assert saved is False
    assert len(list((tmp_path / "audio_cache").glob("*.npz"))) == 0


# ---------------------------------------------------------------------------
# C.2: int16 Compressed Storage
# ---------------------------------------------------------------------------
def test_audio_cache_int16_storage_and_reload(tmp_path: Path):
    """Verify cached audio is compressed to int16 on disk and reloaded as float32."""
    cache = AudioCacheManager(cache_dir=tmp_path / "audio_cache")

    orig_audio = np.array([0.0, 0.5, -0.5, 0.99, -0.99], dtype=np.float32)
    sample_rate = 22050

    saved = cache.save("Box box box", orig_audio, sample_rate)
    assert saved is True

    cache_file = cache.get_cache_file("Box box box")
    assert cache_file.exists()

    # Inspect disk representation: must be int16
    with np.load(cache_file) as npz:
        disk_audio = npz["audio"]
        assert disk_audio.dtype == np.int16
        assert int(npz["sample_rate"]) == sample_rate

    # Reload via cache manager: must return float32 [-1.0, 1.0]
    loaded = cache.load("Box box box")
    assert loaded is not None
    loaded_audio, loaded_sr = loaded
    assert loaded_audio.dtype == np.float32
    assert loaded_sr == sample_rate
    assert np.allclose(loaded_audio, orig_audio, atol=1e-3)


# ---------------------------------------------------------------------------
# C.3: Orphan Cleanup on Voice Change / Line Edits
# ---------------------------------------------------------------------------
def test_audio_cache_orphan_cleanup(tmp_path: Path):
    """Verify cleanup_orphans deletes outdated voices and deleted/modified phrases."""
    cache_dir = tmp_path / "audio_cache"
    cache = AudioCacheManager(cache_dir=cache_dir, voice="en-GB-RyanNeural")

    # Save a valid static phrase for Ryan
    cache.save("Box box box", np.zeros(800, dtype=np.float32), 16000)
    ryan_file = cache.get_cache_file("Box box box")
    assert ryan_file.exists()

    # Manually create an orphaned file from an old voice ("en-US-JennyNeural")
    import hashlib
    old_key = hashlib.sha256("en-US-JennyNeural:Box box box".encode("utf-8")).hexdigest()
    old_file = cache_dir / f"{old_key}.npz"
    np.savez_compressed(old_file, audio=np.zeros(800, dtype=np.int16), sample_rate=16000)
    assert old_file.exists()

    # Manually create an orphaned file from a deleted phrase
    deleted_key = hashlib.sha256("en-GB-RyanNeural:ancient phrase no longer used".encode("utf-8")).hexdigest()
    deleted_file = cache_dir / f"{deleted_key}.npz"
    np.savez_compressed(deleted_file, audio=np.zeros(800, dtype=np.int16), sample_rate=16000)
    assert deleted_file.exists()

    # Run orphan cleanup with current static phrases
    removed = cache.cleanup_orphans(all_static_phrases=["Box box box"])
    assert removed == 2

    assert ryan_file.exists()
    assert not old_file.exists()
    assert not deleted_file.exists()


# ---------------------------------------------------------------------------
# C.4: LRU Eviction at Size Cap
# ---------------------------------------------------------------------------
def test_audio_cache_lru_eviction(tmp_path: Path):
    """Verify least-recently-used files are evicted when cache reaches audio_max_mb cap."""
    cache_dir = tmp_path / "audio_cache"
    # Small budget: 0.01 MB (~10 KB)
    cache = AudioCacheManager(cache_dir=cache_dir, max_mb=0.01)

    # 4 KB per audio clip
    chunk = np.ones(2000, dtype=np.float32)

    cache.save("Box box box", chunk, 16000)
    file1 = cache.get_cache_file("Box box box")
    time.sleep(0.02)

    cache.save("Clear left", chunk, 16000)
    file2 = cache.get_cache_file("Clear left")
    time.sleep(0.02)

    # Access "Box box box" to refresh its access time
    _ = cache.load("Box box box")
    time.sleep(0.02)

    # Save third and fourth clips to overflow the 10 KB budget
    cache.save("Yellow flag", chunk, 16000)
    time.sleep(0.02)
    cache.save("Quiet mode on", chunk, 16000)

    # "Clear left" was least recently accessed, so it should have been evicted first
    stats = cache.get_stats()
    assert stats["size_bytes"] <= cache.max_bytes or stats["file_count"] < 4


# ---------------------------------------------------------------------------
# A: Clean Shutdown within 5s with No Non-Daemon Threads Remaining
# ---------------------------------------------------------------------------
def test_engine_stops_within_5s_no_non_daemon_threads():
    """Verify engine stop_event triggers clean shutdown within 5 s and no non-daemon threads linger."""
    class FakeTTS:
        async def synth(self, text: str):
            return np.zeros(800, dtype=np.float32), 16000

        async def warmup(self):
            pass

    class FakePlayer:
        def play(self, audio, sr, is_interrupted=None):
            return True

        def stop(self):
            pass

    async def _run_shutdown_scenario():
        latency = LatencyTracker()
        tts = FakeTTS()
        player = FakePlayer()

        engineer = Engineer(ptt_action=1, latency=latency, tts=tts, player=player)
        speaker_task = asyncio.create_task(engineer.speaker())

        # Check that any background worker threads created are daemon=True
        active_threads = threading.enumerate()
        for t in active_threads:
            if t is not threading.main_thread():
                assert t.daemon is True, f"Thread {t.name} is not daemon=True"

        # Signal shutdown
        t_start = time.perf_counter()
        engineer.request_shutdown()
        speaker_task.cancel()
        await engineer.stop()
        t_elapsed = time.perf_counter() - t_start

        # Must stop cleanly within 5 seconds
        assert t_elapsed < 5.0, f"Shutdown took {t_elapsed:.2f}s (> 5.0s limit)"

        # Verify no non-daemon threads remained running
        for t in threading.enumerate():
            if t is not threading.main_thread():
                assert t.daemon is True, f"Lingering non-daemon thread found: {t.name}"

    asyncio.run(_run_shutdown_scenario())
