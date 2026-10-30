"""Unit tests for mini-sector tracking, comparison, and F1 sector colors (Prompt 22).

Covers:
1. 10m grid interpolation handling negative distance before start line
2. Mini-sector times from synthetic series (25 equal segments)
3. F1 sector colors (purple = session best, green = personal best, yellow = slower)
4. Field view ("fastest by mini-sector") and wins legend
5. Theoretical best lap calculation and potential gain
6. Exclusion of flagged laps (SC/VSC, pit in/out, invalid, traffic)
7. Restricted telemetry handling (your_telemetry = 0)
8. Payload size cap (< 25 KB)
9. Deterministic voice intents ("where am I slow", "who's fastest in sector 2", "what's my ideal lap")
"""
import json
import os
import tempfile
import pytest

from f1telemetry.state import RaceState
from f1telemetry.deterministic import route
from f1telemetry.packets import Participant
from minisectors import (
    MiniSectorStorage,
    MiniSectorTracker,
    LapTimingRecord,
    DEFAULT_MINISECTORS,
)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def storage(temp_db):
    return MiniSectorStorage(temp_db)


@pytest.fixture
def tracker(storage):
    return MiniSectorTracker(storage=storage, num_minisectors=DEFAULT_MINISECTORS)


def test_minisector_interpolation_with_negative_distance(tracker):
    """Verifies that samples with negative distance before the line are cleanly interpolated."""
    track_len = 5000.0
    lap_time_ms = 90000

    # Synthetic series starting before the start/finish line (-15.0m, -5.0m)
    samples = [
        (0.0, 0, 180.0, 1.0, 0.0),
        (250.0, 4500, 200.0, 1.0, 0.0),
        (1000.0, 18000, 250.0, 1.0, 0.0),
        (2500.0, 45000, 120.0, 0.2, 0.8),
        (3800.0, 68000, 280.0, 1.0, 0.0),
        (5000.0, 90000, 240.0, 1.0, 0.0),
    ]

    times_ms, speeds, throttles, brakes = tracker.interpolate_lap_samples(samples, track_len, lap_time_ms)
    num_bins = int(track_len // 10.0) + 1

    assert len(times_ms) == num_bins
    assert len(speeds) == num_bins
    assert len(throttles) == num_bins
    assert len(brakes) == num_bins

    # Times must start at 0, end at lap_time_ms, and be strictly monotonic
    assert times_ms[0] == 0
    assert times_ms[-1] == lap_time_ms
    for i in range(1, len(times_ms)):
        assert times_ms[i] >= times_ms[i - 1]


def test_compute_minisector_times(tracker):
    """Verifies 25 mini-sector segment durations sum to total lap time."""
    track_len = 5000.0
    lap_time_ms = 100000
    num_bins = int(track_len // 10.0) + 1

    # Linear timing grid: 100,000 ms over 501 bins
    times_ms = [int(i * (lap_time_ms / (num_bins - 1))) for i in range(num_bins)]
    rec = LapTimingRecord(
        session_uid="sess_1",
        car_idx=0,
        lap_num=1,
        lap_time_ms=lap_time_ms,
        s1_ms=30000,
        s2_ms=35000,
        s3_ms=35000,
        is_valid=True,
        is_pit=False,
        is_sc=False,
        is_traffic=False,
        telemetry_restricted=False,
        num_bins=num_bins,
        times_ms=times_ms,
        speed_kph=[200.0] * num_bins,
        throttle=[1.0] * num_bins,
        brake=[0.0] * num_bins,
    )

    seg_times = tracker.compute_minisector_times(rec, track_len, num_minisectors=25)
    assert len(seg_times) == 25
    assert sum(seg_times) == pytest.approx(lap_time_ms, abs=100)
    for t in seg_times:
        assert t > 0


def test_sector_and_minisector_f1_colors(tracker, storage):
    """Validates F1-style sector colors: purple (session best), green (personal best), yellow (slower)."""
    track_len = 5000.0
    num_bins = int(track_len // 10.0) + 1

    # Car 0 Lap 1: Baseline (85.0s, S1=25.0s, S2=30.0s, S3=30.0s)
    t0_1 = [int(i * (85000 / (num_bins - 1))) for i in range(num_bins)]
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_f1", car_idx=0, lap_num=1, lap_time_ms=85000,
        s1_ms=25000, s2_ms=30000, s3_ms=30000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=t0_1,
        speed_kph=[], throttle=[], brake=[],
    ))

    # Car 0 Lap 2: Personal best (83.0s, S1=24.5s, S2=29.5s, S3=29.0s)
    t0_2 = [int(i * (83000 / (num_bins - 1))) for i in range(num_bins)]
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_f1", car_idx=0, lap_num=2, lap_time_ms=83000,
        s1_ms=24500, s2_ms=29500, s3_ms=29000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=t0_2,
        speed_kph=[], throttle=[], brake=[],
    ))

    # Car 1 Lap 1: Session best in S1 & S2 (81.0s, S1=23.5s, S2=28.5s, S3=29.0s)
    t1_1 = [int(i * (81000 / (num_bins - 1))) for i in range(num_bins)]
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_f1", car_idx=1, lap_num=1, lap_time_ms=81000,
        s1_ms=23500, s2_ms=28500, s3_ms=29000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=t1_1,
        speed_kph=[], throttle=[], brake=[],
    ))

    # Test Car 0 Lap 2 colors:
    # S1 was beaten by Car 1 -> green (Car 0 PB)
    # S2 was beaten by Car 1 -> green (Car 0 PB)
    # S3 tied session best -> purple
    res = tracker.get_f1_sector_colors("sess_f1", car_idx=0, lap_num=2, track_len_m=track_len)
    sectors = {s["sector"]: s["color"] for s in res["sectors"]}
    assert sectors[1] == "green"
    assert sectors[2] == "green"
    assert sectors[3] == "purple"

    # Test Car 0 Lap 1 colors (slower than its own PB):
    res_l1 = tracker.get_f1_sector_colors("sess_f1", car_idx=0, lap_num=1, track_len_m=track_len)
    sectors_l1 = {s["sector"]: s["color"] for s in res_l1["sectors"]}
    assert sectors_l1[1] == "yellow"
    assert sectors_l1[2] == "yellow"


def test_fastest_by_minisector_and_wins_legend(tracker, storage):
    """Field view assigns fastest driver per mini-sector and tallies legend wins correctly."""
    track_len = 5000.0
    num_bins = int(track_len // 10.0) + 1

    # Car 0 faster in first half (bins 0 to 250)
    times_0 = []
    for i in range(num_bins):
        if i < 250:
            times_0.append(i * 100)
        else:
            times_0.append(25000 + (i - 250) * 200)

    # Car 1 faster in second half (bins 250 to 500)
    times_1 = []
    for i in range(num_bins):
        if i < 250:
            times_1.append(i * 180)
        else:
            times_1.append(45000 + (i - 250) * 80)

    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_field", car_idx=0, lap_num=1, lap_time_ms=times_0[-1],
        s1_ms=25000, s2_ms=25000, s3_ms=25000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times_0,
        speed_kph=[], throttle=[], brake=[],
    ))
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_field", car_idx=1, lap_num=1, lap_time_ms=times_1[-1],
        s1_ms=25000, s2_ms=25000, s3_ms=25000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times_1,
        speed_kph=[], throttle=[], brake=[],
    ))

    drivers_meta = {
        0: {"name": "Norris", "team": "McLaren", "color": "#ff8000"},
        1: {"name": "Verstappen", "team": "Red Bull", "color": "#3671c6"},
    }

    res = tracker.get_fastest_by_minisector("sess_field", track_len, drivers_meta=drivers_meta)
    sectors = res["sectors"]
    legend = res["wins_legend"]

    assert len(sectors) == 25
    # Norris should win early mini-sectors, Verstappen late
    assert sectors[0]["winner_car_idx"] == 0
    assert sectors[0]["winner_name"] == "Norris"
    assert sectors[0]["winner_color"] == "#ff8000"

    assert sectors[-1]["winner_car_idx"] == 1
    assert sectors[-1]["winner_name"] == "Verstappen"

    # Wins legend must tally all 25 segments
    total_wins = sum(d["sectors_won"] for d in legend)
    assert total_wins == 25


def test_theoretical_best_lap(tracker, storage):
    """Calculates driver's ideal lap by summing personal best mini-sectors across different laps."""
    track_len = 5000.0
    num_bins = int(track_len // 10.0) + 1

    # Lap 1: 90.0s total, fast in first 10 sectors (100ms per bin), slow later (300ms per bin)
    t1 = []
    for i in range(num_bins):
        if i < 200:
            t1.append(i * 100)
        else:
            t1.append(20000 + (i - 200) * 300)

    # Lap 2: 90.0s total, slow in first 10 sectors (300ms per bin), fast later (100ms per bin)
    t2 = []
    for i in range(num_bins):
        if i < 200:
            t2.append(i * 300)
        else:
            t2.append(60000 + (i - 200) * 100)

    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_tb", car_idx=0, lap_num=1, lap_time_ms=t1[-1],
        s1_ms=25000, s2_ms=30000, s3_ms=35000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=t1,
        speed_kph=[], throttle=[], brake=[],
    ))
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_tb", car_idx=0, lap_num=2, lap_time_ms=t2[-1],
        s1_ms=35000, s2_ms=30000, s3_ms=25000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=t2,
        speed_kph=[], throttle=[], brake=[],
    ))

    tb = tracker.get_theoretical_best("sess_tb", car_idx=0, track_len_m=track_len)
    assert tb["has_data"] is True
    # Theoretical best must be faster than actual best
    assert tb["theoretical_best_ms"] < tb["actual_best_ms"]
    assert tb["potential_gain_s"] > 0.0
    assert "Your ideal lap is" in tb["summary_text"]


def test_flagged_laps_exclusion(storage):
    """Excludes flagged laps (SC, Pit, Invalid, Traffic) from best calculations by default."""
    num_bins = 100
    times = [i * 1000 for i in range(num_bins)]

    # 1 valid clean lap
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_flag", car_idx=0, lap_num=1, lap_time_ms=90000,
        s1_ms=30000, s2_ms=30000, s3_ms=30000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times,
        speed_kph=[], throttle=[], brake=[],
    ))
    # 4 flagged laps: invalid, pit, sc, traffic
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_flag", car_idx=0, lap_num=2, lap_time_ms=80000,
        s1_ms=25000, s2_ms=25000, s3_ms=30000,
        is_valid=False, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times,
        speed_kph=[], throttle=[], brake=[],
    ))
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_flag", car_idx=0, lap_num=3, lap_time_ms=110000,
        s1_ms=30000, s2_ms=40000, s3_ms=40000,
        is_valid=True, is_pit=True, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times,
        speed_kph=[], throttle=[], brake=[],
    ))
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_flag", car_idx=0, lap_num=4, lap_time_ms=120000,
        s1_ms=40000, s2_ms=40000, s3_ms=40000,
        is_valid=True, is_pit=False, is_sc=True, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times,
        speed_kph=[], throttle=[], brake=[],
    ))
    storage.save_lap_timing(LapTimingRecord(
        session_uid="sess_flag", car_idx=0, lap_num=5, lap_time_ms=95000,
        s1_ms=32000, s2_ms=33000, s3_ms=30000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=True,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times,
        speed_kph=[], throttle=[], brake=[],
    ))

    clean_laps = storage.list_session_laps("sess_flag", include_flagged=False)
    all_laps = storage.list_session_laps("sess_flag", include_flagged=True)

    assert len(clean_laps) == 1
    assert clean_laps[0]["lap_num"] == 1
    assert len(all_laps) == 5


def test_restricted_telemetry_handling(tracker, storage):
    """Handles online drivers with restricted telemetry (yourTelemetry = 0)."""
    track_len = 5000.0
    num_bins = int(track_len // 10.0) + 1
    times = [i * 200 for i in range(num_bins)]

    # Driver A: Full telemetry
    rec_a = LapTimingRecord(
        session_uid="sess_restr", car_idx=0, lap_num=1, lap_time_ms=90000,
        s1_ms=30000, s2_ms=30000, s3_ms=30000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times,
        speed_kph=[250.0] * num_bins,
        throttle=[1.0] * num_bins,
        brake=[0.0] * num_bins,
    )
    storage.save_lap_timing(rec_a)

    # Driver B: Restricted telemetry (your_telemetry = 0)
    rec_b = LapTimingRecord(
        session_uid="sess_restr", car_idx=1, lap_num=1, lap_time_ms=90500,
        s1_ms=30200, s2_ms=30100, s3_ms=30200,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=True, num_bins=num_bins, times_ms=times,
        speed_kph=[], throttle=[], brake=[],
    )
    storage.save_lap_timing(rec_b)

    loaded_b = storage.get_lap_timing("sess_restr", 1, 1)
    assert loaded_b.telemetry_restricted is True
    assert loaded_b.speed_kph == []

    comp = tracker.compare_laps(rec_a, rec_b, track_len)
    assert len(comp["segments"]) == 25
    # Timing comparison works
    assert comp["total_delta_s"] < 0
    # Telemetry min_speed_b_kph is None due to restriction
    assert comp["segments"][0]["min_speed_b_kph"] is None


def test_payload_size_cap(tracker, storage):
    """Ensures comparison and fastest minisector payloads remain under 25 KB."""
    track_len = 5891.0
    num_bins = int(track_len // 10.0) + 1
    times_a = [int(i * (88000 / (num_bins - 1))) for i in range(num_bins)]
    times_b = [int(i * (88500 / (num_bins - 1))) for i in range(num_bins)]

    rec_a = LapTimingRecord(
        session_uid="sess_cap", car_idx=0, lap_num=1, lap_time_ms=88000,
        s1_ms=28000, s2_ms=30000, s3_ms=30000,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times_a,
        speed_kph=[260.0] * num_bins, throttle=[1.0] * num_bins, brake=[0.0] * num_bins,
    )
    rec_b = LapTimingRecord(
        session_uid="sess_cap", car_idx=1, lap_num=1, lap_time_ms=88500,
        s1_ms=28200, s2_ms=30100, s3_ms=30200,
        is_valid=True, is_pit=False, is_sc=False, is_traffic=False,
        telemetry_restricted=False, num_bins=num_bins, times_ms=times_b,
        speed_kph=[258.0] * num_bins, throttle=[1.0] * num_bins, brake=[0.0] * num_bins,
    )
    storage.save_lap_timing(rec_a)
    storage.save_lap_timing(rec_b)

    comp = tracker.compare_laps(rec_a, rec_b, track_len)
    payload_json = json.dumps(comp)
    size_kb = len(payload_json.encode("utf-8")) / 1024.0

    # Must be well below 25 KB
    assert size_kb < 25.0
    assert size_kb < 15.0


def test_deterministic_intents_for_minisectors():
    """Deterministic queries: 'where am I slow', 'who's fastest in sector 2', 'what's my ideal lap'."""
    st = RaceState()

    # 1. "where am I slow" with biggest_loss_segment
    st.biggest_loss_segment = {
        "name": "Turn 4 (The Loop)",
        "delta_s": 0.28,
        "diagnosis": "Braking earlier",
    }
    r = route("where am I slow", st)
    assert r.kind == "answer"
    assert "Turn 4 (The Loop)" in r.text
    assert "0.28 down" in r.text
    assert "braking earlier" in r.text

    # 2. "who's fastest in sector 2"
    st.fastest_sectors = {
        1: {"driver": "Verstappen", "car_idx": 1, "time_s": 27.842},
        2: {"driver": "Norris", "car_idx": 0, "time_s": 34.120},
        3: {"driver": "Leclerc", "car_idx": 16, "time_s": 26.540},
    }
    r2 = route("who's fastest in sector 2", st)
    assert r2.kind == "answer"
    assert "Norris is fastest in Sector 2" in r2.text
    assert "34.120" in r2.text

    # 3. "what's my ideal lap"
    st.my_theoretical_best = {
        "has_data": True,
        "theoretical_best_str": "1:27.312",
        "potential_gain_s": 0.41,
    }
    r3 = route("what's my ideal lap", st)
    assert r3.kind == "answer"
    assert "Your ideal lap is 1:27.312" in r3.text
    assert "0.41 faster" in r3.text
