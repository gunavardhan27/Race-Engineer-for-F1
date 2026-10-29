"""Tests for persistent session storage and lap trace recording."""
import time
import numpy as np
import pytest

from storage import (
    SessionStorage,
    LapTraceCollector,
    compress_trace,
    decompress_trace,
    resample_lap_trace,
)


def test_compress_decompress_trace_roundtrip():
    """Trace compression and decompression must preserve all 9 channels perfectly."""
    n = 100
    trace = {
        "distance": np.linspace(0, 500, n, dtype=np.float32),
        "speed": np.linspace(100, 320, n, dtype=np.float32),
        "throttle": np.linspace(0.2, 1.0, n, dtype=np.float32),
        "brake": np.zeros(n, dtype=np.float32),
        "steer": np.linspace(-0.5, 0.5, n, dtype=np.float32),
        "gear": np.full(n, 6, dtype=np.int8),
        "rpm": np.full(n, 11500, dtype=np.uint16),
        "world_x": np.linspace(10, 200, n, dtype=np.float32),
        "world_z": np.linspace(50, 400, n, dtype=np.float32),
    }

    blob = compress_trace(trace)
    assert isinstance(blob, bytes)
    assert len(blob) > 0

    recovered = decompress_trace(blob)
    for k in trace:
        np.testing.assert_allclose(recovered[k], trace[k], rtol=1e-5)


def test_trace_resampling():
    """Non-uniform raw telemetry points must be resampled onto uniform 5m distance intervals."""
    # Simulating irregular telemetry ticks (e.g. 0m, 3.2m, 7.8m, 12m, 22m)
    raw = [
        (0.0, 150.0, 0.8, 0.0, 0.05, 5, 10000, 10.0, 20.0),
        (3.2, 155.0, 0.9, 0.0, 0.04, 5, 10200, 13.0, 21.0),
        (7.8, 160.0, 1.0, 0.0, 0.00, 5, 10500, 17.0, 23.0),
        (12.5, 170.0, 1.0, 0.0, -0.02, 6, 11000, 22.0, 26.0),
        (25.0, 185.0, 1.0, 0.0, 0.00, 6, 11800, 35.0, 32.0),
    ]

    res = resample_lap_trace(raw, track_length_m=25.0, distance_step=5.0)

    # Expected distance grid: 0, 5, 10, 15, 20, 25
    expected_dists = np.array([0.0, 5.0, 10.0, 15.0, 20.0, 25.0], dtype=np.float32)
    np.testing.assert_allclose(res["distance"], expected_dists, atol=1e-3)
    assert len(res["speed"]) == 6
    assert res["speed"][0] == 150.0
    assert res["speed"][-1] == 185.0
    assert res["gear"][0] == 5
    assert res["gear"][-1] == 6


def test_storage_round_trip(tmp_path):
    """Storage must persist sessions and completed laps with WAL mode and allow concurrent reads."""
    db_file = str(tmp_path / "test_sessions.db")
    storage = SessionStorage(db_file)

    session_uid = "9876543210123456"
    storage.save_session({
        "session_uid": session_uid,
        "started_at": time.time(),
        "game_format": 2025,
        "formula": 0,
        "track_id": 7,
        "session_type": 10,
        "team_id": 4,
        "weather_at_start": 1,
    })

    trace = {
        "distance": np.array([0.0, 5.0, 10.0], dtype=np.float32),
        "speed": np.array([200.0, 210.0, 220.0], dtype=np.float32),
        "throttle": np.array([1.0, 1.0, 1.0], dtype=np.float32),
        "brake": np.array([0.0, 0.0, 0.0], dtype=np.float32),
        "steer": np.array([0.0, 0.0, 0.0], dtype=np.float32),
        "gear": np.array([7, 7, 7], dtype=np.int8),
        "rpm": np.array([11000, 11200, 11400], dtype=np.uint16),
        "world_x": np.array([100.0, 105.0, 110.0], dtype=np.float32),
        "world_z": np.array([200.0, 205.0, 210.0], dtype=np.float32),
    }

    storage.save_completed_lap({
        "session_uid": session_uid,
        "lap_num": 1,
        "lap_time_s": 89.452,
        "sector1_s": 28.120,
        "sector2_s": 35.430,
        "sector3_s": 25.902,
        "is_valid": True,
        "position": 2,
        "tyre_compound": 18,
        "tyre_age": 4,
        "fuel_start_kg": 42.0,
        "fuel_end_kg": 40.2,
        "tyre_wear_start_pct": 10.0,
        "tyre_wear_end_pct": 13.5,
        "stint": 0,
    }, trace=trace)

    # Wait for background queue to flush to disk
    storage._queue.join()

    # Query back
    sessions = storage.get_sessions(track_id=7)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_uid"] == session_uid
    assert s["formula"] == 0
    assert s["track_id"] == 7
    assert s["session_type"] == 10

    laps = storage.get_laps_for_session(s["id"])
    assert len(laps) == 1
    l = laps[0]
    assert l["lap_num"] == 1
    assert l["lap_time_s"] == pytest.approx(89.452)
    assert l["sector1_s"] == pytest.approx(28.120)
    assert l["sector2_s"] == pytest.approx(35.430)
    assert l["sector3_s"] == pytest.approx(25.902)
    assert l["is_valid"] == 1
    assert l["position"] == 2

    saved_trace = storage.get_lap_trace(l["id"])
    assert saved_trace is not None
    assert len(saved_trace["distance"]) == 3
    np.testing.assert_allclose(saved_trace["speed"], [200.0, 210.0, 220.0])

    storage.close()


def test_flashback_discard(tmp_path):
    """Flashbacks must discard partial and subsequent completed laps from SQLite."""
    db_file = str(tmp_path / "test_flashback.db")
    storage = SessionStorage(db_file)
    session_uid = "flashback_test_session"

    storage.save_session({"session_uid": session_uid, "track_id": 3, "game_format": 2025, "formula": 0, "session_type": 10})

    for lap_n in [1, 2, 3]:
        storage.save_completed_lap({
            "session_uid": session_uid,
            "lap_num": lap_n,
            "lap_time_s": 90.0 + lap_n,
            "is_valid": True,
        })

    storage._queue.join()
    sess = storage.get_session_by_uid(session_uid)
    assert len(storage.get_laps_for_session(sess["id"])) == 3

    # Driver flashes back to lap 2
    storage.discard_laps_after(session_uid, lap_num=2)
    storage._queue.join()

    laps_remaining = storage.get_laps_for_session(sess["id"])
    assert len(laps_remaining) == 1
    assert laps_remaining[0]["lap_num"] == 1

    storage.close()


def test_lap_trace_collector_in_lap_rewind(tmp_path):
    """LapTraceCollector must trim buffer if distance suddenly jumps backwards during a lap."""
    db_file = str(tmp_path / "test_collector.db")
    storage = SessionStorage(db_file)
    collector = LapTraceCollector(storage)

    class DummyState:
        session_uid = "sim_test_uid"
        session = None
        player = 0
        my_team_id = 1
        my_participant = None

        class me:
            lap_num = 1
            lap_distance = 0.0

        class my_tel:
            speed_kph = 200
            throttle = 1.0
            brake = 0.0
            steer = 0.0
            gear = 7
            rpm = 11000

        class my_motion:
            x = 0.0
            z = 0.0

        class my_status:
            fuel_in_tank = 50.0
            actual_compound = 18
            tyre_age_laps = 1

        class my_damage:
            tyre_wear = (5.0, 5.0, 6.0, 6.0)

    st = DummyState()

    # Progress 100 meters
    for d in [0.0, 25.0, 50.0, 75.0, 100.0]:
        st.me.lap_distance = d
        collector.on_tick(st)

    assert len(collector._raw_samples) == 5

    # Player flashes back from 100m to 40m
    st.me.lap_distance = 40.0
    collector.on_tick(st)

    # Samples with distance > 40.0 must have been dropped
    assert all(s[0] <= 40.0 for s in collector._raw_samples)
    assert len(collector._raw_samples) == 3  # 0.0, 25.0, 40.0

    storage.close()
