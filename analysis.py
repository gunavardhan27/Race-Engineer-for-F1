"""Lap analysis, delta-to-best computation, corner breakdown, and stint degradation for F1 25.

Provides high-precision telemetry comparisons across a shared distance axis (5-meter grid),
identifies corner apexes, braking points, throttle pickup points, and fits tyre/lap time degradation slopes.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

from storage import SessionStorage

log = logging.getLogger("engineer.analysis")


def compute_delta_trace(
    lap_trace: dict[str, np.ndarray],
    ref_trace: dict[str, np.ndarray],
) -> dict[str, np.ndarray]:
    """Computes time delta (seconds) vs distance between lap_trace and ref_trace.

    Both traces are on a 5-meter distance grid.
    Positive delta means lap_trace is slower than ref_trace; negative means faster.
    """
    d1 = lap_trace.get("distance", np.zeros(0, dtype=np.float32))
    d2 = ref_trace.get("distance", np.zeros(0, dtype=np.float32))

    n = min(len(d1), len(d2))
    if n < 2:
        return {
            "distance": np.zeros(0, dtype=np.float32),
            "delta": np.zeros(0, dtype=np.float32),
            "lap_speed": np.zeros(0, dtype=np.float32),
            "ref_speed": np.zeros(0, dtype=np.float32),
        }

    distances = d1[:n]
    # Speed in m/s, clamped to at least 1.0 m/s (3.6 kph) to prevent division by zero
    s1 = np.maximum(lap_trace["speed"][:n] / 3.6, 1.0)
    s2 = np.maximum(ref_trace["speed"][:n] / 3.6, 1.0)

    # Step distance between adjacent points (usually 5.0m)
    ds = np.diff(distances, prepend=distances[0])
    ds[0] = 5.0

    # Average speed across each interval
    s1_avg = (s1 + np.roll(s1, 1)) / 2.0
    s1_avg[0] = s1[0]
    s2_avg = (s2 + np.roll(s2, 1)) / 2.0
    s2_avg[0] = s2[0]

    # Time elapsed per segment
    dt1 = ds / s1_avg
    dt2 = ds / s2_avg

    # Cumulative time elapsed
    t1_cum = np.cumsum(dt1)
    t2_cum = np.cumsum(dt2)

    delta = t1_cum - t2_cum

    return {
        "distance": distances,
        "delta": delta.astype(np.float32),
        "lap_speed": lap_trace["speed"][:n],
        "ref_speed": ref_trace["speed"][:n],
    }


def find_corner_apexes(
    speeds: np.ndarray,
    distances: np.ndarray,
    min_drop_kph: float = 15.0,
    window_pts: int = 15,
) -> list[int]:
    """Finds apex indices (local speed minima) in a circuit speed trace."""
    n = len(speeds)
    if n < window_pts * 2:
        return []

    apexes: list[int] = []
    i = window_pts
    while i < n - window_pts:
        local_window = speeds[max(0, i - window_pts):min(n, i + window_pts + 1)]
        min_idx = np.argmin(local_window)
        center_idx = max(0, i - window_pts) + min_idx

        # Check if local minimum is significant
        if center_idx == i and speeds[i] < 310.0:
            left_max = np.max(speeds[max(0, i - window_pts * 2):i]) if i > 0 else speeds[i]
            right_max = np.max(speeds[i:min(n, i + window_pts * 2)]) if i < n - 1 else speeds[i]
            if (left_max - speeds[i] >= min_drop_kph) and (right_max - speeds[i] >= min_drop_kph / 2):
                apexes.append(i)
                i += window_pts  # advance past this apex
        i += 1

    return apexes


def analyze_corners(
    lap_trace: dict[str, np.ndarray],
    ref_trace: dict[str, np.ndarray],
    corner_names: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Splits the lap at corners and compares min speed, braking, throttle, and delta gained/lost."""
    delta_res = compute_delta_trace(lap_trace, ref_trace)
    distances = delta_res["distance"]
    delta = delta_res["delta"]
    lap_speed = delta_res["lap_speed"]
    ref_speed = delta_res["ref_speed"]

    n = len(distances)
    if n < 20:
        return []

    lap_brake = lap_trace.get("brake", np.zeros(n))[:n]
    lap_throttle = lap_trace.get("throttle", np.zeros(n))[:n]

    # Use reference lap speed to locate standard circuit apexes
    apex_indices = find_corner_apexes(ref_speed, distances)
    if not apex_indices:
        apex_indices = find_corner_apexes(lap_speed, distances)

    corners: list[dict[str, Any]] = []
    names = corner_names or []

    for idx, a_idx in enumerate(apex_indices):
        d_apex = float(distances[a_idx])

        # Name corner
        if idx < len(names):
            name = names[idx]
        else:
            name = f"Turn {idx + 1}"

        # Search window around apex for min speeds
        w_start = max(0, a_idx - 8)
        w_end = min(n, a_idx + 9)
        min_lap = float(np.min(lap_speed[w_start:w_end]))
        min_ref = float(np.min(ref_speed[w_start:w_end]))
        speed_diff = round(min_lap - min_ref, 1)

        # Braking point: search backwards up to 40 points (200m)
        brake_search_start = max(0, a_idx - 40)
        brake_slice = lap_brake[brake_search_start:a_idx]
        brake_candidates = np.where(brake_slice > 0.05)[0]
        if len(brake_candidates) > 0:
            b_idx = brake_search_start + brake_candidates[0]
            braking_point_m = float(distances[b_idx])
            entry_idx = max(0, b_idx - 2)
        else:
            braking_point_m = round(max(0.0, d_apex - 80.0), 1)
            entry_idx = max(0, a_idx - 16)

        # Throttle pickup point: search forwards up to 30 points (150m)
        throttle_search_end = min(n, a_idx + 30)
        throttle_slice = lap_throttle[a_idx:throttle_search_end]
        throttle_candidates = np.where(throttle_slice > 0.15)[0]
        if len(throttle_candidates) > 0:
            t_idx = a_idx + throttle_candidates[0]
            throttle_pickup_m = float(distances[t_idx])
            exit_idx = min(n - 1, t_idx + 4)
        else:
            throttle_pickup_m = round(min(float(distances[-1]), d_apex + 60.0), 1)
            exit_idx = min(n - 1, a_idx + 16)

        # Time gained/lost through corner region
        time_gained = round(float(delta[exit_idx] - delta[entry_idx]), 3)

        corners.append({
            "corner": name,
            "distance_m": round(d_apex, 1),
            "min_speed_lap": round(min_lap, 1),
            "min_speed_ref": round(min_ref, 1),
            "speed_diff_kph": speed_diff,
            "braking_m": round(braking_point_m, 1),
            "throttle_pickup_m": round(throttle_pickup_m, 1),
            "time_gained_s": time_gained,  # positive = lost time, negative = gained time
        })

    return corners


def analyze_stint(laps: list[dict[str, Any]]) -> dict[str, Any]:
    """Fits degradation slopes for lap times and tyre wear across a stint."""
    valid_laps = [lap for lap in laps if lap.get("is_valid") and lap.get("lap_time_s", 0) > 0]
    if not valid_laps:
        return {
            "lap_count": len(laps),
            "valid_lap_count": 0,
            "time_deg_s_per_lap": 0.0,
            "wear_deg_pct_per_lap": 0.0,
            "laps": laps,
        }

    lap_nums = np.array([lap["lap_num"] for lap in valid_laps], dtype=float)
    lap_times = np.array([lap["lap_time_s"] for lap in valid_laps], dtype=float)
    tyre_wears = np.array([lap.get("tyre_wear_end_pct") or 0.0 for lap in valid_laps], dtype=float)

    # Linear degradation slope for lap time (seconds gained/lost per lap)
    if len(lap_nums) >= 2:
        try:
            time_slope, _ = np.polyfit(lap_nums, lap_times, 1)
        except Exception:
            time_slope = 0.0

        try:
            wear_slope, _ = np.polyfit(lap_nums, tyre_wears, 1)
        except Exception:
            wear_slope = 0.0
    else:
        time_slope = 0.0
        wear_slope = 0.0

    return {
        "lap_count": len(laps),
        "valid_lap_count": len(valid_laps),
        "time_deg_s_per_lap": round(float(time_slope), 3),
        "wear_deg_pct_per_lap": round(float(wear_slope), 2),
        "laps": valid_laps,
    }


def get_lap_comparison_summary(
    storage: SessionStorage,
    track_id: int,
    formula: int,
    session_type: int,
    last_lap: Optional[dict[str, Any]] = None,
    corner_names: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Builds 'personal_best' and 'last_lap_vs_best' structures for the live snapshot."""
    pb = storage.get_personal_best(track_id, formula=formula, session_type=session_type)
    if not pb:
        return {
            "personal_best": None,
            "last_lap_vs_best": None,
        }

    pb_summary = {
        "lap_id": pb["id"],
        "lap_time_s": pb["lap_time_s"],
        "lap_num": pb["lap_num"],
        "session_id": pb["session_id"],
    }

    if not last_lap or not last_lap.get("id") or last_lap.get("id") == pb["id"]:
        return {
            "personal_best": pb_summary,
            "last_lap_vs_best": None,
        }

    delta_total = round(float(last_lap["lap_time_s"] - pb["lap_time_s"]), 3)

    # Fetch traces and perform corner analysis
    last_trace = storage.get_lap_trace(last_lap["id"])
    pb_trace = storage.get_lap_trace(pb["id"])

    worst_corner = None
    if last_trace and pb_trace:
        corners = analyze_corners(last_trace, pb_trace, corner_names=corner_names)
        if corners:
            # Worst corner is where time_gained_s is maximum positive (most time lost)
            worst = max(corners, key=lambda c: c["time_gained_s"])
            worst_corner = {
                "name": worst["corner"],
                "delta_s": worst["time_gained_s"],
                "speed_diff_kph": worst["speed_diff_kph"],
                "distance_m": worst["distance_m"],
            }

    return {
        "personal_best": pb_summary,
        "last_lap_vs_best": {
            "delta_s": delta_total,
            "worst_corner": worst_corner,
        },
    }
