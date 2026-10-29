"""Tests for lap analysis, delta-to-best computation, corner breakdown, and stint degradation."""
import numpy as np
import pytest

from analysis import (
    analyze_corners,
    analyze_stint,
    compute_delta_trace,
    find_corner_apexes,
    get_lap_comparison_summary,
)
from storage import SessionStorage


def test_delta_computation_on_synthetic_laps():
    """Delta calculation must accurately calculate time difference along a shared distance axis."""
    n = 200
    distances = np.linspace(0, 1000, n, dtype=np.float32)  # 1000m total

    # Lap 1: Constant 180 kph (50 m/s) -> 1000m takes 20.0s
    trace1 = {
        "distance": distances,
        "speed": np.full(n, 180.0, dtype=np.float32),
        "throttle": np.ones(n, dtype=np.float32),
        "brake": np.zeros(n, dtype=np.float32),
    }

    # Lap 2 (Ref): Constant 216 kph (60 m/s) -> 1000m takes 16.67s
    trace2 = {
        "distance": distances,
        "speed": np.full(n, 216.0, dtype=np.float32),
        "throttle": np.ones(n, dtype=np.float32),
        "brake": np.zeros(n, dtype=np.float32),
    }

    res = compute_delta_trace(trace1, trace2)
    assert len(res["delta"]) == n

    # Lap 1 is slower than Lap 2 (Ref) -> delta must be positive and growing
    assert res["delta"][0] >= 0.0
    final_delta = res["delta"][-1]
    expected_delta = 20.0 - 16.6667
    assert final_delta == pytest.approx(expected_delta, rel=0.05)


def test_corner_split_and_apex_detection():
    """Corner analysis must detect apex minimum speed, braking point, and throttle pickup."""
    n = 200
    distances = np.linspace(0, 1000, n, dtype=np.float32)  # 5m steps

    # Create a circuit with a straight, a hairpin at 400m, and another straight
    speeds_ref = np.full(n, 280.0, dtype=np.float32)
    # Slow down around index 80 (400m)
    speeds_ref[60:100] = np.linspace(280, 100, 20).tolist() + np.linspace(100, 280, 20).tolist()

    speeds_lap = np.array(speeds_ref, copy=True)
    # Lap 1 takes the corner slower (90 kph instead of 100 kph)
    speeds_lap[75:85] = np.linspace(120, 90, 5).tolist() + np.linspace(90, 120, 5).tolist()

    brakes = np.zeros(n, dtype=np.float32)
    brakes[55:75] = 1.0  # heavy braking before apex at index 80

    throttles = np.ones(n, dtype=np.float32)
    throttles[55:85] = 0.0  # off throttle in braking & apex
    throttles[85:95] = 0.8  # throttle pickup at index 85 (425m)

    lap_trace = {"distance": distances, "speed": speeds_lap, "brake": brakes, "throttle": throttles}
    ref_trace = {"distance": distances, "speed": speeds_ref, "brake": brakes, "throttle": throttles}

    corners = analyze_corners(lap_trace, ref_trace, corner_names=["Turn 1 (Hairpin)"])
    assert len(corners) == 1
    c = corners[0]
    assert c["corner"] == "Turn 1 (Hairpin)"
    assert c["distance_m"] == pytest.approx(400.0, abs=15.0)
    assert c["min_speed_lap"] == pytest.approx(90.0, abs=2.0)
    assert c["min_speed_ref"] == pytest.approx(100.0, abs=2.0)
    assert c["speed_diff_kph"] == pytest.approx(-10.0, abs=2.0)
    assert c["braking_m"] < c["distance_m"]
    assert c["throttle_pickup_m"] > c["distance_m"]
    assert c["time_gained_s"] > 0  # Lap was slower, so lost time (positive delta)


def test_stint_degradation_fit():
    """Stint analysis must accurately calculate lap time and tyre degradation slopes."""
    # Synthetic 5-lap stint: lap times degrade by +0.15s per lap, tyre wear degrades by +1.8% per lap
    synthetic_laps = [
        {"lap_num": 1, "lap_time_s": 90.00, "is_valid": True, "tyre_wear_end_pct": 5.0},
        {"lap_num": 2, "lap_time_s": 90.15, "is_valid": True, "tyre_wear_end_pct": 6.8},
        {"lap_num": 3, "lap_time_s": 90.30, "is_valid": True, "tyre_wear_end_pct": 8.6},
        {"lap_num": 4, "lap_time_s": 90.45, "is_valid": True, "tyre_wear_end_pct": 10.4},
        {"lap_num": 5, "lap_time_s": 90.60, "is_valid": True, "tyre_wear_end_pct": 12.2},
    ]

    res = analyze_stint(synthetic_laps)
    assert res["lap_count"] == 5
    assert res["valid_lap_count"] == 5
    assert res["time_deg_s_per_lap"] == pytest.approx(0.15, rel=1e-3)
    assert res["wear_deg_pct_per_lap"] == pytest.approx(1.80, rel=1e-3)


def test_get_lap_comparison_summary(tmp_path):
    """Personal best and worst corner summary must be extracted correctly for snapshot integration."""
    db_file = str(tmp_path / "test_comp_summary.db")
    storage = SessionStorage(db_file)
    session_uid = "comp_test_uid"

    storage.save_session({"session_uid": session_uid, "track_id": 7, "formula": 0, "session_type": 10})

    # Lap 1 (PB): 89.0s
    n = 100
    dists = np.linspace(0, 500, n, dtype=np.float32)
    trace_pb = {
        "distance": dists,
        "speed": np.full(n, 250.0, dtype=np.float32),
        "brake": np.zeros(n, dtype=np.float32),
        "throttle": np.ones(n, dtype=np.float32),
        "steer": np.zeros(n, dtype=np.float32),
        "gear": np.full(n, 7, dtype=np.int8),
        "rpm": np.full(n, 11000, dtype=np.uint16),
        "world_x": dists,
        "world_z": dists,
    }
    # Add a corner to trace_pb around index 50
    trace_pb["speed"][40:60] = np.linspace(250, 120, 10).tolist() + np.linspace(120, 250, 10).tolist()

    storage.save_completed_lap({
        "session_uid": session_uid,
        "lap_num": 1,
        "lap_time_s": 89.00,
        "is_valid": True,
    }, trace=trace_pb)

    # Lap 2 (Current, slower): 89.35s
    trace_slow = dict(trace_pb)
    trace_slow["speed"] = np.array(trace_pb["speed"], copy=True)
    trace_slow["speed"][45:55] = 105.0  # slower in the corner

    storage.save_completed_lap({
        "session_uid": session_uid,
        "lap_num": 2,
        "lap_time_s": 89.35,
        "is_valid": True,
    }, trace=trace_slow)

    storage._queue.join()

    laps = storage.get_laps_for_session(1)
    summary = get_lap_comparison_summary(
        storage,
        track_id=7,
        formula=0,
        session_type=10,
        last_lap=laps[1],
        corner_names=["Copse"],
    )

    assert summary["personal_best"] is not None
    assert summary["personal_best"]["lap_time_s"] == 89.00
    assert summary["last_lap_vs_best"] is not None
    assert summary["last_lap_vs_best"]["delta_s"] == pytest.approx(0.35, rel=1e-2)
    assert summary["last_lap_vs_best"]["worst_corner"] is not None
    assert summary["last_lap_vs_best"]["worst_corner"]["name"] == "Copse"

    storage.close()
