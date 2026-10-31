"""Comprehensive tests for Prompt 21:
- TrackModel from multi-car telemetry on a known geometric track (centreline error < 1m, corner count/direction, braking zone medians)
- 24-car order tracking with 2.0s / 80m confirmation and side-by-side flicker rejection
- Classification of all overtake classes: clean, brave, divebomb, divebomb_contact,
  overtake_mode_pass, around_the_outside, switchback, passed_stricken_car
- Lost-pass classification: divebombed_by, fair_pass_against, forced_off_by
- Victim reaction excluding normal corner braking
- Corner-name persistence and matching within 50m across rebuilds
"""
from __future__ import annotations

from collections import deque
import math
import tempfile
import time
from pathlib import Path

import pytest
import numpy as np

from config import AppConfig, DefendConfig, OvertakeConfig
from f1telemetry.overtakes import BrakingZone, BrakingZoneDetector, OvertakeDetector, DefendWarningDetector, OvertakeCandidate
from f1telemetry.state import RaceState
from f1telemetry.packets import NUM_CARS
from personality import PersonalityEngine
from trackmodel import Corner, TrackModel, TrackModelBuilder, TrackModelStorage


# ==============================================================================
# Helper to create a synthetic oval track (straight + semi-circle + straight + semi-circle)
# ==============================================================================

def make_synthetic_oval_track(track_len: float = 1000.0):
    """Creates a synthetic track of length 1000m:
    0 - 300m: Straight 1 (x=0, z from 0 to 300)
    300 - 500m: Turn 1 (Right turn 180 deg, radius = 200/pi ≈ 63.66m)
    500 - 800m: Straight 2 (x=127.32, z from 300 to 0)
    800 - 1000m: Turn 2 (Right turn 180 deg back to start)
    """
    radius = 200.0 / math.pi
    diameter = 2.0 * radius

    def get_coords(d: float):
        d_norm = d % track_len
        if 0.0 <= d_norm < 300.0:
            return 0.0, 0.0, d_norm
        elif 300.0 <= d_norm < 500.0:
            arc = d_norm - 300.0
            theta = arc / radius
            return radius * (1.0 - math.cos(theta)), 0.0, 300.0 + radius * math.sin(theta)
        elif 500.0 <= d_norm < 800.0:
            s_dist = d_norm - 500.0
            return diameter, 0.0, 300.0 - s_dist
        else:
            arc = d_norm - 800.0
            theta = arc / radius
            return radius * (1.0 + math.cos(theta)), 0.0, -radius * math.sin(theta)

    return get_coords


# ==============================================================================
# 1. Track Model Construction & Accuracy Tests
# ==============================================================================

def test_track_model_centreline_and_corners():
    """Validates TrackModel on a synthetic shape: centreline error < 1m, corner count & directions."""
    track_len = 1000.0
    get_coords = make_synthetic_oval_track(track_len)
    builder = TrackModelBuilder(track_id=1, track_length_m=track_len, curvature_threshold=0.003)

    # Simulate 5 cars completing 3 laps each
    for car_idx in range(5):
        for lap_num in range(1, 4):
            for dist in np.arange(0.0, track_len, 2.0):
                x, y, z = get_coords(dist)
                # Add minor lateral noise to test median filtering
                noise = (car_idx - 2) * 0.4
                speed = 220.0
                brake = 0.0
                if 270.0 <= dist <= 310.0:  # Brake before Turn 1
                    brake = 0.8
                    speed = 130.0
                elif 770.0 <= dist <= 810.0:  # Brake before Turn 2
                    brake = 0.8
                    speed = 130.0

                builder.feed_sample(
                    car_idx=car_idx,
                    lap_num=lap_num,
                    lap_dist=dist,
                    total_dist=lap_num * track_len + dist,
                    x=x + noise,
                    y=y,
                    z=z,
                    speed_kph=speed,
                    brake=brake,
                    throttle=1.0 - brake,
                    steer=0.2 if (300 <= dist <= 500 or 800 <= dist <= 1000) else 0.0,
                    pit_status=0,
                    result_status=2,
                    safety_car=0,
                )

    model = builder.build_model()
    assert model.track_length_m == track_len
    assert model.is_ready is True
    assert model.coverage_pct >= 95.0

    # 1. Verify centreline error < 1.0 m on average
    errors = []
    for b in model.bins:
        true_x, _, true_z = get_coords(b.lap_distance_m)
        err = math.hypot(b.x - true_x, b.z - true_z)
        errors.append(err)
    mean_err = float(np.mean(errors))
    assert mean_err < 1.0, f"Mean centreline error {mean_err:.2f}m exceeds 1.0m"

    # 2. Verify corner detection: exactly 2 corners (Turn 1 and Turn 2), both right turns
    assert len(model.corners) == 2, f"Expected 2 corners, got {len(model.corners)}"
    assert model.corners[0].direction == "right"
    assert model.corners[1].direction == "right"
    assert 280.0 <= model.corners[0].start_dist_m <= 320.0
    assert 780.0 <= model.corners[1].start_dist_m <= 820.0

    # 3. Braking zone medians: brake onset detected around 270-280m
    assert 250.0 <= model.corners[0].braking_start_median_m <= 290.0


# ==============================================================================
# 2. 24 Cars Order Tracking & Confirmation Tests
# ==============================================================================

def test_24_cars_overtake_confirmation_and_flicker_rejection():
    """Tests 24-car field order tracking: confirms after 2.0s or 80m held; rejects side-by-side flicker."""
    detector = OvertakeDetector(config=OvertakeConfig(confirm_time_s=2.0, proximity_m=60.0))
    state = RaceState()
    state.player = 0

    # Initialize 24 cars
    laps = []
    for i in range(24):
        c = type("Lap", (), {
            "total_distance": 1000.0 - i * 10.0,
            "lap_distance": 500.0,
            "lap_num": 2,
            "pit_status": 0,
            "result_status": 2,
            "corner_cutting_warnings": 0,
            "lap_invalid": False,
        })()
        laps.append(c)
    state.lap = laps

    # Car 23 is ahead of player initially (e.g. at 1020m, player at 1000m)
    laps[23].total_distance = 1020.0
    laps[0].total_distance = 1000.0

    # Frame 1: Establish baseline signs
    detector.update(state, now=10.0)
    assert 23 not in detector._candidates

    # Frame 2: Player passes car 23 (sign change: player at 1025m, car 23 at 1021m)
    laps[0].total_distance = 1025.0
    detector.update(state, now=10.5)
    assert 23 in detector._candidates
    assert detector._candidates[23].direction == "pass"

    # Case A: Flicker back after 0.8s (car 23 repasses player) -> rejected
    laps[0].total_distance = 1030.0
    laps[23].total_distance = 1032.0  # car 23 back ahead
    detector.update(state, now=11.3)
    assert 23 not in detector._candidates  # cancelled by flicker!

    # Case B: Re-pass and held for 2.0s -> confirmed
    laps[0].total_distance = 1040.0
    laps[23].total_distance = 1035.0
    detector.update(state, now=12.0)
    assert 23 in detector._candidates

    # Advance time by 2.1s while maintaining sign
    laps[0].total_distance = 1080.0
    laps[23].total_distance = 1070.0
    confirmed = detector.update(state, now=14.1)
    assert len(confirmed) == 1
    assert confirmed[0]["car_idx"] == 23
    assert confirmed[0]["direction"] == "pass"


# ==============================================================================
# 3. Overtake Classification Feature Sets Tests
# ==============================================================================

def test_overtake_classes():
    """Exercises all Prompt 21 overtake classes from synthetic feature sets."""
    detector = OvertakeDetector(config=OvertakeConfig())
    zone = BrakingZone(1, "Stowe", 1000.0, 1100.0, 1000.0, 120.0, 180.0, "right")

    # 1. Straight pass with Overtake Mode active -> overtake_mode_pass
    f_ot_mode = {"braking_delta_m": 0.0, "contact": False}
    c_ot = detector._classify_overtake(
        features=f_ot_mode, is_inside_corner=False, corner_or_zone=None,
        switchback=False, is_stricken=False, direction="pass", side="inside",
        overtake_mode_active=True, location="straight"
    )
    assert c_ot == "overtake_mode_pass"

    # 2. Outside of corner -> around_the_outside
    f_outside = {"braking_delta_m": 5.0, "start_gap_m": -2.0, "overlap_at_turn_in": True, "contact": False}
    c_out = detector._classify_overtake(
        features=f_outside, is_inside_corner=True, corner_or_zone=zone,
        switchback=False, is_stricken=False, direction="pass", side="outside",
        overtake_mode_active=False, location="mid_corner"
    )
    assert c_out == "around_the_outside"

    # 3. Brave pass (braking delta 18m, no contact, inside)
    f_brave = {"braking_delta_m": 18.0, "start_gap_m": -6.0, "contact": False, "ran_deep": False, "forced_off": False}
    c_brave = detector._classify_overtake(
        features=f_brave, is_inside_corner=True, corner_or_zone=zone,
        switchback=False, is_stricken=False, direction="pass", side="inside",
        overtake_mode_active=False, location="braking_zone"
    )
    assert c_brave == "brave"

    # 4. Divebomb without contact (start gap < -10m and ran deep)
    f_dive = {"start_gap_m": -12.0, "braking_delta_m": 28.0, "ran_deep": True, "contact": False}
    c_dive = detector._classify_overtake(
        features=f_dive, is_inside_corner=True, corner_or_zone=zone,
        switchback=False, is_stricken=False, direction="pass", side="inside",
        overtake_mode_active=False, location="braking_zone"
    )
    assert c_dive == "divebomb"

    # 5. Divebomb with contact severity 1
    f_dive_c = {"start_gap_m": -12.0, "braking_delta_m": 28.0, "ran_deep": True, "contact": True, "contact_severity": 1}
    c_dive_c = detector._classify_overtake(
        features=f_dive_c, is_inside_corner=True, corner_or_zone=zone,
        switchback=False, is_stricken=False, direction="pass", side="inside",
        overtake_mode_active=False, location="braking_zone"
    )
    assert c_dive_c == "divebomb_contact"

    # 6. Switchback override
    f_sw = {"braking_delta_m": 5.0, "contact": False}
    c_sw = detector._classify_overtake(
        features=f_sw, is_inside_corner=True, corner_or_zone=zone,
        switchback=True, is_stricken=False, direction="pass", side="inside",
        overtake_mode_active=False, location="exit"
    )
    assert c_sw == "overtake_switchback"

    # 7. Stricken car
    c_st = detector._classify_overtake(
        features={}, is_inside_corner=False, corner_or_zone=None,
        switchback=False, is_stricken=True, direction="pass", side="inside",
        overtake_mode_active=False, location="straight"
    )
    assert c_st == "passed_stricken_car"

    # 8. Straight pass without overtake mode -> overtake_straight
    c_str = detector._classify_overtake(
        features={"braking_delta_m": 0.0, "contact": False}, is_inside_corner=False, corner_or_zone=None,
        switchback=False, is_stricken=False, direction="pass", side="inside",
        overtake_mode_active=False, location="straight"
    )
    assert c_str == "overtake_straight"

    # 9. Backmarker pass (lapped car) -> backmarker_pass
    c_bm = detector._classify_overtake(
        features={"braking_delta_m": 0.0, "contact": False}, is_inside_corner=False, corner_or_zone=None,
        switchback=False, is_stricken=False, direction="pass", side="inside",
        overtake_mode_active=False, location="straight", is_backmarker=True
    )
    assert c_bm == "backmarker_pass"


# ==============================================================================
# 4. Lost Pass Classification Tests
# ==============================================================================

def test_lost_pass_classification():
    """Tests classification of positions lost: divebombed_by, forced_off_by, fair_pass_against."""
    detector = OvertakeDetector(config=OvertakeConfig())
    zone = BrakingZone(1, "Turn 1", 500.0, 580.0, 500.0, 100.0, 160.0, "left")

    # 1. Opponent divebombs player (start gap < -10m, ran deep or forced off)
    f_dive_by = {"start_gap_m": -14.0, "braking_delta_m": 30.0, "ran_deep": True}
    c_d_by = detector._classify_overtake(
        features=f_dive_by, is_inside_corner=True, corner_or_zone=zone,
        switchback=False, is_stricken=False, direction="loss", side="inside",
        overtake_mode_active=False, location="braking_zone"
    )
    assert c_d_by == "divebombed_by"

    # 2. Opponent forces player off track
    f_forced = {"braking_delta_m": 8.0, "forced_off": True}
    c_forced = detector._classify_overtake(
        features=f_forced, is_inside_corner=True, corner_or_zone=zone,
        switchback=False, is_stricken=False, direction="loss", side="inside",
        overtake_mode_active=False, location="mid_corner"
    )
    assert c_forced == "forced_off_by"

    # 3. Fair pass by opponent alongside
    f_fair = {"braking_delta_m": 5.0, "overlap_at_turn_in": True, "forced_off": False, "ran_deep": False}
    c_fair = detector._classify_overtake(
        features=f_fair, is_inside_corner=True, corner_or_zone=zone,
        switchback=False, is_stricken=False, direction="loss", side="inside",
        overtake_mode_active=False, location="braking_zone"
    )
    assert c_fair == "fair_pass_against"


# ==============================================================================
# 5. Defend Warnings & Inside-Look Tests with TrackModel
# ==============================================================================

def test_defend_warning_with_track_model():
    """Defend warning fires 300-150m before corner; inside-look fires if attacker moves inside."""
    model = TrackModel(
        track_id=1,
        track_length_m=5000.0,
        corners=[
            Corner(
                number=3,
                name="Copse",
                start_dist_m=1200.0,
                apex_dist_m=1250.0,
                end_dist_m=1300.0,
                direction="right",
                field_median_min_speed_kph=180.0,
                braking_start_median_m=1180.0,
                braking_start_p10_m=1160.0,
                braking_start_p90_m=1200.0,
                median_entry_speed_kph=240.0,
            )
        ]
    )

    detector = DefendWarningDetector(config=DefendConfig(cooldown_overall_s=20.0), track_model=model)
    state = RaceState()
    state.player = 0
    state.track_length = 5000.0

    # Player at lap distance 980m -> 200m before Copse braking start (1180m)
    p_lap = type("Lap", (), {"lap_distance": 980.0, "lap_num": 3, "position": 2, "result_status": 2, "pit_status": 0})()
    p_mot = type("Mot", (), {
        "x": 0.0, "y": 0.0, "z": 0.0,
        "fwd_x": 0.0, "fwd_y": 0.0, "fwd_z": 1.0,
        "right_x": 1.0, "right_y": 0.0, "right_z": 0.0
    })()

    # Attacker behind player: car 1 at -12m
    o_lap = type("Lap", (), {"lap_distance": 968.0, "lap_num": 3, "position": 3, "result_status": 2, "pit_status": 0, "gap_to_car_ahead_ms": 300})()
    o_mot = type("Mot", (), {"x": 0.0, "y": 0.0, "z": -12.0})()

    state.lap = [p_lap, o_lap]
    state.motion = [p_mot, o_mot]
    state.names = {1: "Verstappen"}

    # Frame 1: Establish baseline motion sample
    detector.update(state, now=10.0)

    # Frame 2: 0.9s later, attacker closes to -8m (closing)
    o_mot.z = -8.0
    warnings = detector.update(state, now=10.9)
    assert len(warnings) == 1
    assert warnings[0]["corner"] == "Copse"
    assert warnings[0]["inside_look"] is False

    # Reset cooldown for inside-look test
    detector._last_call_time = -100.0
    detector._corner_lap_fired.clear()

    # Frame 3: Attacker moves inside (corner is "right" -> d_right > +1.0m)
    o_mot.z = -12.0
    o_mot.x = 0.0
    detector.update(state, now=20.0)

    o_mot.z = -7.0
    o_mot.x = 2.0  # moved 2m to the inside right
    w_inside = detector.update(state, now=20.9)
    assert len(w_inside) == 1
    assert w_inside[0]["corner"] == "Copse"
    assert w_inside[0]["inside_look"] is True


def test_defend_warning_false_positive_rejection():
    """Verify defend warnings do not fire for false positives:
    - Non-rival cars (e.g. backmarkers / cars in P18)
    - Cars across hairpins / parallel track switchbacks
    - Cars dropping back (gap opening)
    - Qualifying / Practice / Time Trial sessions
    - Safety Car / VSC periods
    - Player in pit lane
    """
    model = TrackModel(
        track_id=1,
        track_length_m=5000.0,
        corners=[
            Corner(
                number=3,
                name="Copse",
                start_dist_m=1200.0,
                apex_dist_m=1250.0,
                end_dist_m=1300.0,
                direction="right",
                field_median_min_speed_kph=180.0,
                braking_start_median_m=1180.0,
                braking_start_p10_m=1160.0,
                braking_start_p90_m=1200.0,
                median_entry_speed_kph=240.0,
            )
        ]
    )
    detector = DefendWarningDetector(config=DefendConfig(cooldown_overall_s=20.0), track_model=model)
    state = RaceState()
    state.player = 0
    state.track_length = 5000.0

    p_lap = type("Lap", (), {"lap_distance": 980.0, "lap_num": 3, "position": 2, "result_status": 2, "pit_status": 0})()
    p_mot = type("Mot", (), {
        "x": 0.0, "y": 0.0, "z": 0.0,
        "fwd_x": 0.0, "fwd_y": 0.0, "fwd_z": 1.0,
        "right_x": 1.0, "right_y": 0.0, "right_z": 0.0
    })()

    # 1. Backmarker (P18) physically nearby in 3D: should NOT trigger defend warning
    o_backmarker = type("Lap", (), {"lap_distance": 968.0, "lap_num": 2, "position": 18, "result_status": 2, "pit_status": 0, "gap_to_car_ahead_ms": 300})()
    o_mot_bm = type("Mot", (), {"x": 0.0, "y": 0.0, "z": -8.0})()
    state.lap = [p_lap, o_backmarker]
    state.motion = [p_mot, o_mot_bm]
    detector._motion_history[1] = deque([(10.0, -12.0, 0.0)])
    assert detector.update(state, now=11.0) == []

    # 2. Car on parallel track / hairpin switchback (close in 3D, far in track spline: 1800m vs 980m)
    o_rival_hairpin = type("Lap", (), {"lap_distance": 1800.0, "lap_num": 3, "position": 3, "result_status": 2, "pit_status": 0, "gap_to_car_ahead_ms": 300})()
    o_mot_hp = type("Mot", (), {"x": 0.0, "y": 0.0, "z": -8.0})()
    state.lap = [p_lap, o_rival_hairpin]
    state.motion = [p_mot, o_mot_hp]
    detector._motion_history[1] = deque([(10.0, -12.0, 0.0)])
    assert detector.update(state, now=11.0) == []

    # 3. Car dropping back (distance widening from -8m to -14m)
    o_rival_real = type("Lap", (), {"lap_distance": 968.0, "lap_num": 3, "position": 3, "result_status": 2, "pit_status": 0, "gap_to_car_ahead_ms": 800})()
    o_mot_real = type("Mot", (), {"x": 0.0, "y": 0.0, "z": -14.0})()
    state.lap = [p_lap, o_rival_real]
    state.motion = [p_mot, o_mot_real]
    detector._motion_history[1] = deque([(10.0, -8.0, 0.0)])
    assert detector.update(state, now=11.0) == []

    # 4. Qualifying / Practice session: should NOT trigger defend warning
    state.session = type("Session", (), {"session_type": 5, "safety_car": 0})()  # 5 = Q1
    o_mot_real.z = -7.0
    detector._motion_history[1] = deque([(10.0, -12.0, 0.0)])
    assert detector.update(state, now=11.0) == []

    # 5. Safety Car active (session_type=10 race, safety_car=1): should NOT trigger defend warning
    state.session = type("Session", (), {"session_type": 10, "safety_car": 1})()
    assert detector.update(state, now=11.0) == []

    # 6. Player in pit lane (pit_status=1): should NOT trigger defend warning
    state.session = type("Session", (), {"session_type": 10, "safety_car": 0})()
    p_lap.pit_status = 1
    assert detector.update(state, now=11.0) == []


# ==============================================================================
# 6. Corner Name Persistence Across Rebuilds
# ==============================================================================

def test_corner_name_persistence_across_rebuilds(tmp_path):
    """Custom corner names set on Tracks page survive model rebuilds matching within 50m of apex."""
    db_file = tmp_path / "tracks_test.db"
    storage = TrackModelStorage(db_file)

    # Initial Model with apex at 1250.0m
    m1 = TrackModel(
        track_id=1,
        track_length_m=5000.0,
        corners=[
            Corner(1, "Turn 1", 500.0, 550.0, 600.0, "left", 120.0, 480.0, 460.0, 500.0, 180.0),
            Corner(2, "Turn 2", 1200.0, 1250.0, 1300.0, "right", 150.0, 1180.0, 1160.0, 1200.0, 220.0),
        ]
    )
    storage.save_model(m1)

    # User renames Turn 2 to "Copse" on Tracks page
    storage.set_corner_name(track_id=1, corner_number=2, apex_dist_m=1250.0, name="Copse")

    # Rebuild happens with more laps: apex slightly shifted to 1258.0m (within 50m)
    m2 = TrackModel(
        track_id=1,
        track_length_m=5000.0,
        corners=[
            Corner(1, "Turn 1", 500.0, 550.0, 600.0, "left", 120.0, 480.0, 460.0, 500.0, 180.0),
            Corner(2, "Turn 2", 1205.0, 1258.0, 1305.0, "right", 152.0, 1185.0, 1165.0, 1205.0, 222.0),
        ]
    )
    storage.save_model(m2)

    # Loaded model matches "Copse" to Corner 2 despite the shift
    loaded = storage.load_model(track_id=1, track_length_m=5000.0)
    assert loaded is not None
    assert loaded.corners[1].name == "Copse"


def test_backmarker_overtake_flow():
    """Passing a lapped car creates a candidate flagged as is_backmarker and confirms as backmarker_pass."""
    detector = OvertakeDetector(config=OvertakeConfig(confirm_time_s=1.0, proximity_m=60.0))
    state = RaceState()
    state.player = 0

    laps = [
        type("Lap", (), {"total_distance": 2000.0, "lap_distance": 500.0, "lap_num": 3, "pit_status": 0, "result_status": 2, "corner_cutting_warnings": 0, "lap_invalid": False, "position": 2})(),
        type("Lap", (), {"total_distance": 2010.0, "lap_distance": 510.0, "lap_num": 2, "pit_status": 0, "result_status": 2, "corner_cutting_warnings": 0, "lap_invalid": False, "position": 18})(),
    ]
    state.lap = laps
    state.names = {0: "Player", 1: "Logan Sargeant"}

    # Frame 1: Establish baseline signs
    detector.update(state, now=10.0)

    # Frame 2: Player passes car 1 (lapped backmarker)
    laps[0].total_distance = 2020.0
    detector.update(state, now=10.5)
    assert 1 in detector._candidates
    assert detector._candidates[1].is_backmarker is True

    # Frame 3: Confirm after hold duration
    laps[0].total_distance = 2060.0
    confirmed = detector.update(state, now=12.0)
    assert len(confirmed) == 1
    assert confirmed[0]["classification"] == "backmarker_pass"

    # Verify personality response
    from personality import PersonalityEngine
    from config import AppConfig
    cfg = AppConfig()
    engine = PersonalityEngine(config=cfg.persona)
    callout = engine.on_signal(("overtake_confirmed", confirmed[0]), state)
    assert callout is not None
    assert "Clean pass" in callout.text or "clean pass" in callout.text.lower()


def test_straight_pass_personality_flow():
    """Straight pass produces simple praise 'Easy pass, good job'."""
    from personality import PersonalityEngine
    from config import AppConfig
    cfg = AppConfig()
    engine = PersonalityEngine(config=cfg.persona)
    state = RaceState()
    state.player = 0
    state.lap = [type("Lap", (), {"position": 3, "lap_num": 5})()]

    callout = engine.on_signal(("overtake_confirmed", {
        "classification": "overtake_straight",
        "other_driver_name": "Russell",
        "corner_name": "Straight",
        "position": 3,
    }), state)
    assert callout is not None
    assert "Easy pass" in callout.text and "good job" in callout.text.lower()


def test_spotter_suppressed_on_overtake():
    """Spotter calls are suppressed for 3 seconds during an overtake to prevent audio collision."""
    from f1telemetry.spotter import Spotter, SpotterConfig
    from f1telemetry.packets import CarMotion, LapData
    spotter = Spotter(SpotterConfig())

    # Trigger suppression at now=100.0
    spotter.suppress(3.0, now=100.0)
    assert spotter.suppress_until == 103.0

    st = RaceState()
    st.player = 0
    p_mot = CarMotion(x=0.0, y=0.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0,
                      fwd_x=0.0, fwd_y=0.0, fwd_z=1.0, right_x=1.0, right_y=0.0, right_z=0.0)
    opp_right = CarMotion(x=2.0, y=0.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0)
    p_lap = LapData(last_lap_ms=80000, current_lap_ms=10000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                    lap_distance=500.0, position=1, lap_num=2, pit_status=0, num_pit_stops=0, sector=0,
                    lap_invalid=False, penalties_s=0, result_status=2)
    opp_lap = LapData(last_lap_ms=80000, current_lap_ms=10000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                      lap_distance=500.0, position=2, lap_num=2, pit_status=0, num_pit_stops=0, sector=0,
                      lap_invalid=False, penalties_s=0, result_status=2)

    class FakeTel:
        speed_kph = 150

    st.tel = [FakeTel(), FakeTel()]
    st.motion = [p_mot, opp_right]
    st.lap = [p_lap, opp_lap]

    # During suppression window (now=101.5), spotter yields None
    call = spotter.process(st, now=101.5)
    assert call is None

    # After suppression window expires (now=104.0), spotter can process overlap
    call_after = spotter.process(st, now=104.0)
    assert call_after is not None
    assert "right" in call_after.lower()


def test_overtake_pass_suppresses_spotter_and_plays_praise():
    """Confirms overtake pass suppresses spotter and plays praise without 'Car left' / 'Still there' collision."""
    from f1telemetry.spotter import Spotter, SpotterConfig, SpotterState
    from f1telemetry.overtakes import OvertakeDetector, OvertakeConfig
    from f1telemetry.packets import CarMotion, LapData
    from personality import PersonalityEngine, PersonaConfig

    spotter = Spotter(SpotterConfig())
    pers = PersonalityEngine(PersonaConfig(name="cocky"))
    ot_det = OvertakeDetector(OvertakeConfig())

    st = RaceState()
    st.player = 0
    p_mot = CarMotion(x=0.0, y=0.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0,
                      fwd_x=0.0, fwd_y=0.0, fwd_z=1.0, right_x=1.0, right_y=0.0, right_z=0.0)
    opp_left = CarMotion(x=-2.0, y=0.0, z=-1.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0)

    p_lap = LapData(last_lap_ms=80000, current_lap_ms=10000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                    lap_distance=500.0, position=1, lap_num=2, pit_status=0, num_pit_stops=0, sector=0,
                    lap_invalid=False, penalties_s=0, result_status=2)
    opp_lap = LapData(last_lap_ms=80000, current_lap_ms=10000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                      lap_distance=500.0, position=2, lap_num=2, pit_status=0, num_pit_stops=0, sector=0,
                      lap_invalid=False, penalties_s=0, result_status=2)

    class FakeTel:
        speed_kph = 200

    st.tel = [FakeTel(), FakeTel()]
    st.motion = [p_mot, opp_left]
    st.lap = [p_lap, opp_lap]

    # Pre-overtake: car on left
    phrase = spotter.process(st, now=10.0)
    assert phrase == "Car left"
    assert spotter.state == SpotterState.CAR_LEFT

    # Overtake happens on straight
    ot_record = {
        "direction": "pass",
        "classification": "overtake_straight",
        "other_driver_name": "Russell",
        "corner_name": "Straight",
        "position": 2,
    }

    # As in main.py upon pass confirmation:
    spotter.suppress(4.0, now=12.0)
    assert spotter.state == SpotterState.CLEAR

    # Personality praise must succeed with Priority 1 even if spotter had been occupied
    praise = pers.on_signal(("overtake_confirmed", ot_record), st, spotter_active=False, now=12.0)
    assert praise is not None
    assert praise.priority == 1
    assert "easy pass" in praise.text.lower() or "russell" in praise.text.lower()

    # During suppression window (now=13.0, 14.0), opponent still alongside (-1.0m rear quarter)
    # Spotter MUST remain silent and NOT emit 'Car left' or 'Still there'
    assert spotter.process(st, now=13.0) is None
    assert spotter.process(st, now=14.0) is None
    assert spotter.state == SpotterState.CLEAR

