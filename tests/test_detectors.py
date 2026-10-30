"""Comprehensive unit and integration tests for deterministic detectors, finish logic,
overtakes, defend warnings, session reset handling, dashboard persistence, and PENA decoding.
"""
from __future__ import annotations

import json
import logging
import struct
import tempfile
import time
from collections import deque
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from config import AppConfig, FinishConfig, OvertakeConfig, DefendConfig
from f1telemetry import packets
from f1telemetry.deterministic import Callouts, RadioQueue, Callout
from f1telemetry.overtakes import BrakingZone, BrakingZoneDetector, OvertakeDetector, DefendWarningDetector
from f1telemetry.packets import (
    HEADER, LAP, MOTION, NUM_CARS, PacketId, PENALTY_TYPES, INFRINGEMENT_TYPES,
)
from f1telemetry.spotter import Spotter, SpotterState
from f1telemetry.state import RaceState, LapRecord
from personality import PersonalityEngine
from storage import SessionStorage
from dashboard.server import create_app, Broadcaster
from main import Engineer, ExplainTracker, replay, FRAME


def make_header(pid: int, uid: int = 12345, session_time: float = 100.0, player_idx: int = 0) -> bytes:
    """Builds 29-byte F1 25 header."""
    return HEADER.pack(2025, 25, 1, 0, 1, pid, uid, session_time, 0, 0, player_idx, 255)


def make_lap_packet(
    cars: dict[int, dict],
    uid: int = 12345,
    session_time: float = 100.0,
    player_idx: int = 0,
) -> bytes:
    """Builds synthetic LAP_DATA packet with customizable per-car fields."""
    body = b""
    for i in range(NUM_CARS):
        c = cars.get(i, {})
        # LAP fields:
        # 0: last_lap_ms, 1: current_lap_ms, 2: s1_ms, 3: s1_part, 4: s2_ms, 5: s2_part, 6: delta_ahead_ms,
        # 7: delta_ahead_part, 8: delta_car_race_ms, 9: delta_car_part, 10: lap_dist, 11: total_dist,
        # 12: sc_delta, 13: pos, 14: lap_num, 15: pit_status, 16: num_pit_stops, 17: sector, 18: current_lap_invalid,
        # 19: pens_s, 20: warnings, 21: corner_cut_warn, 22: unserved_dt, 23: unserved_sg, 24: grid_pos,
        # 25: driver_status, 26: result_status, 27: pit_lane_timer_active, 28: pit_lane_time_in_lane_ms,
        # 29: pit_stop_timer_ms, 30: pit_stop_should_serve_pen, 31: speed_trap_kph, 32: speed_trap_part
        row = [0] * 33
        row[10] = float(c.get("lap_distance", 500.0))
        row[11] = float(c.get("total_distance", 1500.0))
        row[13] = int(c.get("position", i + 1))
        row[14] = int(c.get("lap_num", 1))
        row[15] = int(c.get("pit_status", 0))
        row[18] = int(c.get("lap_invalid", 0))
        row[19] = int(c.get("penalties_s", 0))
        row[21] = int(c.get("corner_cutting_warnings", 0))
        row[26] = int(c.get("result_status", 2))   # default active
        row[6] = int(c.get("gap_ahead_ms", 1000))
        row[0] = int(c.get("last_lap_ms", 90000))
        body += LAP.pack(*row)
    return make_header(PacketId.LAP_DATA, uid=uid, session_time=session_time, player_idx=player_idx) + body + b"\xff\xff"


def make_motion_packet(
    cars: dict[int, dict],
    uid: int = 12345,
    session_time: float = 100.0,
    player_idx: int = 0,
) -> bytes:
    """Builds synthetic MOTION packet."""
    body = b""
    for i in range(NUM_CARS):
        c = cars.get(i, {})
        x = float(c.get("x", 0.0))
        y = float(c.get("y", 0.0))
        z = float(c.get("z", 0.0))
        fwd_x = float(c.get("fwd_x", 0.0))
        fwd_y = float(c.get("fwd_y", 0.0))
        fwd_z = float(c.get("fwd_z", 1.0))
        rt_x = float(c.get("rt_x", 1.0))
        rt_y = float(c.get("rt_y", 0.0))
        rt_z = float(c.get("rt_z", 0.0))
        row = (x, y, z, 0.0, 0.0, 0.0, 0, 0, 32767, 32767, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        body += MOTION.pack(*row)
    trailer = bytes(120)
    return make_header(PacketId.MOTION, uid=uid, session_time=session_time, player_idx=player_idx) + body + trailer


def make_pena_packet(
    penalty_type: int,
    infringement_type: int,
    vehicle_idx: int = 0,
    other_vehicle_idx: int = 255,
    penalty_time: int = 255,
    lap_num: int = 1,
    places_gained: int = 0,
    uid: int = 12345,
    session_time: float = 100.0,
) -> bytes:
    """Builds PENA event packet with all 7 payload fields."""
    payload = struct.pack(
        "<BBBBBBB",
        penalty_type,
        infringement_type,
        vehicle_idx,
        other_vehicle_idx,
        penalty_time,
        lap_num,
        places_gained,
    )
    # EVENT packet: 4 bytes event code + 12 bytes event payload
    event_body = b"PENA" + payload.ljust(12, b"\x00")
    return make_header(PacketId.EVENT, uid=uid, session_time=session_time) + event_body


# ==============================================================================
# 1. Session Reset Tests
# ==============================================================================

def test_session_uid_zero_does_not_reset_state():
    """Packets with session_uid == 0 must be ignored entirely and never reset state."""
    state = RaceState()
    pkt_start = make_lap_packet({0: {"position": 1, "lap_num": 5}}, uid=9999)
    h_start, d_start = packets.parse(pkt_start)
    state.update(h_start, d_start)
    assert state.session_uid == 9999
    assert state.me.position == 1

    # Menu/results packet with session_uid == 0
    pkt_zero = make_lap_packet({0: {"position": 10, "lap_num": 1}}, uid=0)
    h_zero, d_zero = packets.parse(pkt_zero)
    sigs = state.update(h_zero, d_zero)

    assert sigs == []
    assert state.session_uid == 9999
    assert state.me.position == 1  # Unchanged!


def test_session_reset_only_on_nonzero_uid_change(caplog):
    """Session reset occurs only when session_uid changes to a non-zero value, logging old and new."""
    state = RaceState()
    pkt1 = make_lap_packet({0: {"position": 3}}, uid=1001)
    h1, d1 = packets.parse(pkt1)
    state.update(h1, d1)
    assert state.session_uid == 1001

    with caplog.at_level(logging.INFO, logger="engineer.detectors"):
        pkt2 = make_lap_packet({0: {"position": 2}}, uid=2002)
        h2, d2 = packets.parse(pkt2)
        sigs = state.update(h2, d2)

    assert any(s[0] == "new_session" for s in sigs)
    assert state.session_uid == 2002
    assert "old_uid=1001, new_uid=2002" in caplog.text


# ==============================================================================
# 2. Finish Detection & Personality Line Tests
# ==============================================================================

def test_finish_detection_margin_buckets():
    """Detects finish on LapData result_status 2->3 with correct margin bucket selection."""
    pers = PersonalityEngine(config=AppConfig().persona)
    state = RaceState()

    # 1. P1 Win Photo Finish (<0.5s margin behind)
    cars_active = {0: {"position": 1, "result_status": 2, "gap_ahead_ms": 0}, 1: {"position": 2, "result_status": 2, "gap_ahead_ms": 350}}
    h_act, d_act = packets.parse(make_lap_packet(cars_active, uid=111))
    state.update(h_act, d_act)

    cars_fin = {0: {"position": 1, "result_status": 3, "gap_ahead_ms": 0}, 1: {"position": 2, "result_status": 3, "gap_ahead_ms": 350}}
    h_fin, d_fin = packets.parse(make_lap_packet(cars_fin, uid=111))
    sigs = state.update(h_fin, d_fin)

    assert any(s[0] == "finished" for s in sigs)
    fin_sig = [s for s in sigs if s[0] == "finished"][0]
    c = pers.on_signal(fin_sig, state, now=10.0)
    assert c is not None
    assert any(word in c.text.lower() for word in ["whisker", "photo", "half a tenth", "line", "tenth", "close", "finish"])

    # 2. P1 Win Dominant (>10s margin behind)
    pers2 = PersonalityEngine(config=AppConfig().persona)
    state2 = RaceState()
    c_act = {0: {"position": 1, "result_status": 2, "gap_ahead_ms": 0}, 1: {"position": 2, "result_status": 2, "gap_ahead_ms": 14500}}
    h2_act, d2_act = packets.parse(make_lap_packet(c_act, uid=222))
    state2.update(h2_act, d2_act)
    c_fin = {0: {"position": 1, "result_status": 3, "gap_ahead_ms": 0}, 1: {"position": 2, "result_status": 3, "gap_ahead_ms": 14500}}
    h2_fin, d2_fin = packets.parse(make_lap_packet(c_fin, uid=222))
    sigs2 = state2.update(h2_fin, d2_fin)
    fin_sig2 = [s for s in sigs2 if s[0] == "finished"][0]
    c2 = pers2.on_signal(fin_sig2, state2, now=10.0)
    assert c2 is not None
    assert any(word in c2.text.lower() for word in ["class", "annihilation", "league", "clear", "destroyed", "dominant", "demolition"])

    # 3. P2 Podium Held (car behind < 1s)
    pers3 = PersonalityEngine(config=AppConfig().persona)
    state3 = RaceState()
    c3_act = {0: {"position": 2, "result_status": 2, "gap_ahead_ms": 3500}, 1: {"position": 3, "result_status": 2, "gap_ahead_ms": 600}}
    h3_act, d3_act = packets.parse(make_lap_packet(c3_act, uid=333))
    state3.update(h3_act, d3_act)
    c3_fin = {0: {"position": 2, "result_status": 3, "gap_ahead_ms": 3500}, 1: {"position": 3, "result_status": 3, "gap_ahead_ms": 600}}
    h3_fin, d3_fin = packets.parse(make_lap_packet(c3_fin, uid=333))
    sigs3 = state3.update(h3_fin, d3_fin)
    fin_sig3 = [s for s in sigs3 if s[0] == "finished"][0]
    c3 = pers3.on_signal(fin_sig3, state3, now=10.0)
    assert c3 is not None
    assert any(w in c3.text.lower() for w in ["held off", "kept", "defence", "podium"])

    # 4. P4 Missed Podium (ahead within 2s)
    pers4 = PersonalityEngine(config=AppConfig().persona)
    state4 = RaceState()
    c4_act = {0: {"position": 4, "result_status": 2, "gap_ahead_ms": 1200}, 1: {"position": 5, "result_status": 2, "gap_ahead_ms": 5000}}
    h4_act, d4_act = packets.parse(make_lap_packet(c4_act, uid=444))
    state4.update(h4_act, d4_act)
    c4_fin = {0: {"position": 4, "result_status": 3, "gap_ahead_ms": 1200}, 1: {"position": 5, "result_status": 3, "gap_ahead_ms": 5000}}
    h4_fin, d4_fin = packets.parse(make_lap_packet(c4_fin, uid=444))
    sigs4 = state4.update(h4_fin, d4_fin)
    fin_sig4 = [s for s in sigs4 if s[0] == "finished"][0]
    c4 = pers4.on_signal(fin_sig4, state4, now=10.0)
    assert c4 is not None
    assert any(w in c4.text.lower() for w in ["p4", "podium", "silverware", "rostrum"])


def test_finish_provisional_penalty_suffix():
    """Unserved time penalties at the finish append a provisional result notice."""
    pers = PersonalityEngine(config=AppConfig().persona)
    state = RaceState()

    cars_act = {0: {"position": 1, "result_status": 2, "penalties_s": 5, "gap_ahead_ms": 0}, 1: {"position": 2, "result_status": 2, "gap_ahead_ms": 4000}}
    h_act, d_act = packets.parse(make_lap_packet(cars_act, uid=777))
    state.update(h_act, d_act)

    cars_fin = {0: {"position": 1, "result_status": 3, "penalties_s": 5, "gap_ahead_ms": 0}, 1: {"position": 2, "result_status": 3, "gap_ahead_ms": 4000}}
    h_fin, d_fin = packets.parse(make_lap_packet(cars_fin, uid=777))
    sigs = state.update(h_fin, d_fin)

    fin_sig = [s for s in sigs if s[0] == "finished"][0]
    c = pers.on_signal(fin_sig, state, now=10.0)
    assert c is not None
    assert "provisional" in c.text.lower()
    assert "five-second penalty" in c.text.lower()


def test_single_finish_message_despite_events_and_suppression():
    """Only one finish message per session; suppresses other lines for 8s; FC only speaks if pos changes."""
    pers = PersonalityEngine(config=AppConfig().persona)
    state = RaceState()

    # Active packet first so state starts in active status 2
    cars_act = {0: {"position": 1, "result_status": 2, "gap_ahead_ms": 0}, 1: {"position": 2, "result_status": 2, "gap_ahead_ms": 2000}}
    h_act, d_act = packets.parse(make_lap_packet(cars_act, uid=888))
    state.update(h_act, d_act)

    # Finish packet
    cars_fin = {0: {"position": 1, "result_status": 3, "gap_ahead_ms": 0}, 1: {"position": 2, "result_status": 3, "gap_ahead_ms": 2000}}
    h_fin, d_fin = packets.parse(make_lap_packet(cars_fin, uid=888))
    sigs = state.update(h_fin, d_fin)
    fin_sig = [s for s in sigs if s[0] == "finished"][0]
    c1 = pers.on_signal(fin_sig, state, now=100.0)
    assert c1 is not None

    # CHQF event packet arrives right after finish -> must NOT trigger a second message
    ev_chqf = packets.Event("CHQF", 0)
    c_chqf = pers.on_signal(("event", ev_chqf), state, now=101.0)
    assert c_chqf is None

    # RCWN event packet arrives -> must NOT trigger a second message
    ev_rcwn = packets.Event("RCWN", 0)
    c_rcwn = pers.on_signal(("event", ev_rcwn), state, now=102.0)
    assert c_rcwn is None

    # Track limits warning arrives 3s after finish -> SUPPRESSED by 8s finish cooldown
    c_suppressed = pers.on_signal(("track_limits_warning", 1), state, now=103.0)
    assert c_suppressed is None

    # FinalClassification arrives with SAME position P1 -> No correction callout
    fc_same = [packets.FinalClassification(position=1, num_laps=50, grid_position=1, points=25, num_pit_stops=1, result_status=3, best_lap_time_ms=88000, total_race_time=5000.0, penalties_time=0, num_penalties=0)]
    c_fc_same = pers.on_signal(("final_classification", fc_same), state, now=104.0)
    assert c_fc_same is None

    # FinalClassification arrives with DIFFERENT position P2 (demoted by penalty) -> speaks correction
    pers._finish_line_pos = 1
    fc_diff = [packets.FinalClassification(position=2, num_laps=50, grid_position=1, points=18, num_pit_stops=1, result_status=3, best_lap_time_ms=88000, total_race_time=5005.0, penalties_time=5, num_penalties=1)]
    c_fc_diff = pers.on_signal(("final_classification", fc_diff), state, now=105.0)
    assert c_fc_diff is not None
    assert "correction" in c_fc_diff.text.lower() or "p2" in c_fc_diff.text.lower()


# ==============================================================================
# 3. Overtake Detection & Filtering Tests
# ==============================================================================

def test_overtake_confirmation_vs_side_by_side_flicker():
    """Sign flip on total_distance confirms only if held for 2.0s; flicker under 2.0s is ignored."""
    state = RaceState()
    det = OvertakeDetector(config=OvertakeConfig(confirm_time_s=2.0))

    # T=0: Car 1 ahead of Player (car 0) by 5m (total_distance player=1000m, other=1005m)
    pkt0 = make_lap_packet({0: {"total_distance": 1000.0, "position": 2}, 1: {"total_distance": 1005.0, "position": 1}}, session_time=10.0)
    h0, d0 = packets.parse(pkt0)
    state.update(h0, d0)
    ots0 = det.update(state, now=10.0)
    assert ots0 == []

    # T=1.0s: Player pulls slightly ahead (player=1025m, other=1023m) -> Candidate pass created
    pkt1 = make_lap_packet({0: {"total_distance": 1025.0, "position": 2}, 1: {"total_distance": 1023.0, "position": 1}}, session_time=11.0)
    h1, d1 = packets.parse(pkt1)
    state.update(h1, d1)
    ots1 = det.update(state, now=11.0)
    assert ots1 == []  # Held for only 0s from candidate start

    # T=2.0s (1.0s after candidate start): Car 1 edges back ahead -> FLICKER!
    pkt2 = make_lap_packet({0: {"total_distance": 1045.0, "position": 2}, 1: {"total_distance": 1046.0, "position": 1}}, session_time=12.0)
    h2, d2 = packets.parse(pkt2)
    state.update(h2, d2)
    ots2 = det.update(state, now=12.0)
    assert ots2 == []  # Candidate cancelled, not confirmed!

    # T=3.0s: Player passes cleanly and holds for 2.5s
    pkt3 = make_lap_packet({0: {"total_distance": 1070.0, "position": 2}, 1: {"total_distance": 1066.0, "position": 1}}, session_time=13.0)
    h3, d3 = packets.parse(pkt3)
    state.update(h3, d3)
    det.update(state, now=13.0)

    # T=5.5s (2.5s held): Pass confirmed!
    pkt4 = make_lap_packet({0: {"total_distance": 1130.0, "position": 1}, 1: {"total_distance": 1115.0, "position": 2}}, session_time=15.5)
    h4, d4 = packets.parse(pkt4)
    state.update(h4, d4)
    ots_conf = det.update(state, now=15.5)
    assert len(ots_conf) == 1
    assert ots_conf[0]["direction"] == "pass"
    assert ots_conf[0]["other_vehicle_idx"] == 1


def test_overtake_filters_pit_lapped_stricken_safety_car():
    """Ignores pit lane, lapped cars, safety car, start (<200m), and classifies stricken cars."""
    state = RaceState()
    det = OvertakeDetector()

    # 1. Car in pit lane
    state.track_length = 5000
    pkt_pit = make_lap_packet({0: {"total_distance": 2000.0}, 1: {"total_distance": 1995.0, "pit_status": 1}})
    h_pit, d_pit = packets.parse(pkt_pit)
    state.update(h_pit, d_pit)
    det._prev_signs[1] = -1
    ots = det.update(state, now=10.0)
    assert ots == []

    # 2. Race start distance < 200m
    pkt_start = make_lap_packet({0: {"total_distance": 150.0}, 1: {"total_distance": 140.0}})
    h_st, d_st = packets.parse(pkt_start)
    state.update(h_st, d_st)
    det._prev_signs[1] = -1
    ots_st = det.update(state, now=10.0)
    assert ots_st == []

    # 3. Lapped car (|total distance diff| near track length)
    pkt_lap = make_lap_packet({0: {"total_distance": 15000.0}, 1: {"total_distance": 10005.0}})
    h_lap, d_lap = packets.parse(pkt_lap)
    state.update(h_lap, d_lap)
    det._prev_signs[1] = -1
    ots_lap = det.update(state, now=10.0)
    assert ots_lap == []

    # 4. Stricken car (<60% player speed for 3s)
    det_str = OvertakeDetector()
    state_str = RaceState()
    # Inject speed history: player @ 250 kph, other car @ 80 kph (<60%)
    det_str._player_speed_history.append((10.0, 250.0))
    det_str._speed_history[1] = deque([(10.0, 80.0), (12.0, 85.0), (13.0, 80.0)])
    det_str._prev_signs[1] = -1

    pkt_str_0 = make_lap_packet({0: {"total_distance": 3000.0}, 1: {"total_distance": 2995.0}}, session_time=10.0)
    h_s0, d_s0 = packets.parse(pkt_str_0)
    state_str.update(h_s0, d_s0)
    det_str.update(state_str, now=10.0)

    # Confirm after 2.1s
    pkt_str_1 = make_lap_packet({0: {"total_distance": 3150.0}, 1: {"total_distance": 3050.0}}, session_time=12.2)
    h_s1, d_s1 = packets.parse(pkt_str_1)
    state_str.update(h_s1, d_s1)
    ots_stricken = det_str.update(state_str, now=12.2)
    assert len(ots_stricken) == 1
    assert ots_stricken[0]["classification"] == "passed_stricken_car"


# ==============================================================================
# 4. Overtake Classification & Switchback Tests
# ==============================================================================

def test_overtake_classes_with_features():
    """Verifies classification: clean, brave, divebomb, divebomb_contact."""
    bz_det = BrakingZoneDetector()
    zone = BrakingZone(
        corner_number=3,
        name="Turn 3",
        start_dist_m=1000.0,
        apex_dist_m=1120.0,
        ref_braking_start_m=1000.0,
        ref_min_speed_kph=100.0,
        ref_exit_speed_kph=180.0,
        direction="right",
    )
    bz_det.set_zones(0, [zone])
    det = OvertakeDetector(braking_zones=bz_det)

    # 1. Clean: pass outside braking zone
    feats_clean = {"braking_delta_m": 0.0, "start_gap_m": 0.0, "overlap_at_turn_in": True, "ran_deep": False, "contact": False, "forced_off": False}
    c_clean = det._classify_overtake(feats_clean, is_inside_braking=False, zone=None)
    assert c_clean == "clean"

    # 2. Brave: braking delta = 15m (10-25m) without contact
    feats_brave = {"braking_delta_m": 15.0, "start_gap_m": -2.0, "overlap_at_turn_in": True, "ran_deep": False, "contact": False, "forced_off": False}
    c_brave = det._classify_overtake(feats_brave, is_inside_braking=True, zone=zone)
    assert c_brave == "brave"

    # 3. Divebomb: start_gap = -9m (< -8m) combined with ran_deep
    feats_dive = {"braking_delta_m": 26.0, "start_gap_m": -9.0, "overlap_at_turn_in": False, "ran_deep": True, "contact": False, "forced_off": False}
    c_dive = det._classify_overtake(feats_dive, is_inside_braking=True, zone=zone)
    assert c_dive == "divebomb"

    # 4. Divebomb with contact
    feats_dive_contact = {"braking_delta_m": 28.0, "start_gap_m": -10.0, "overlap_at_turn_in": False, "ran_deep": True, "contact": True, "forced_off": False}
    c_dive_contact = det._classify_overtake(feats_dive_contact, is_inside_braking=True, zone=zone)
    assert c_dive_contact == "divebomb_contact"


def test_overtake_switchback():
    """If other car re-passes within 5.0s, the pass is classified as overtake_switchback."""
    det = OvertakeDetector()
    state = RaceState()

    # Pass by player on car 2 at T=100.0s
    rec_pass = {
        "session_uid": 123, "lap_num": 1, "timestamp": 100.0, "car_idx": 2,
        "direction": "pass", "classification": "clean", "braking_delta_m": 5.0,
        "start_gap_m": 0.0, "overlap_at_turn_in": 1, "ran_deep": 0, "contact": 0,
        "forced_off": 0, "switchback": 0, "corner_name": "Turn 1",
    }
    det._recent_overtakes.append(rec_pass)

    # Car 2 re-passes player at T=103.5s (within 5.0s)
    cand_loss = type("Cand", (), {
        "direction": "loss", "braking_delta_m": None, "start_gap_m": None,
        "overlap_at_turn_in": False, "player_min_speed": 100, "player_exit_speed": 180,
        "other_pre_apex_speed": None, "other_exit_speed": None, "other_warnings_start": 0,
        "other_invalid_start": False, "is_stricken": False, "braking_zone": None,
    })()
    rec_loss = det._confirm_overtake(state, 2, cand_loss, now=103.5)
    assert rec_loss["classification"] == "overtake_switchback"


def test_told_you_timing_after_divebomb():
    """A real penalty arriving within 10s after a divebomb triggers 'told_you' persona callout."""
    pers = PersonalityEngine(config=AppConfig().persona)
    state = RaceState()

    # 1. Divebomb event on Leclerc at T=50.0s
    sig_dive = ("overtake_confirmed", {
        "classification": "divebomb", "other_driver_name": "Leclerc", "corner_name": "Turn 4", "position": 2
    })
    c_dive = pers.on_signal(sig_dive, state, now=50.0)
    assert c_dive is not None

    # 2. Penalty arrives 6s later (T=56.0s) -> within 10s -> triggers told_you!
    ev_pen = packets.Event("PENA", 0, penalty_type=4, infringement_type=0, vehicle_idx=0, penalty_time=5)
    c_told = pers.on_signal(("penalty", ev_pen), state, now=56.0)
    assert c_told is not None
    assert any(w in c_told.text.lower() for w in ["told you", "divebomb", "lunge"])


# ==============================================================================
# 5. Defend Warning & Inside Look Tests
# ==============================================================================

def test_defend_warning_and_inside_look():
    """Detects defend warning 300-150m before braking zone, and inside look if lateral shift > 1m."""
    bz_det = BrakingZoneDetector()
    zone = BrakingZone(
        corner_number=1,
        name="Abbey",
        start_dist_m=1000.0,
        apex_dist_m=1100.0,
        ref_braking_start_m=1000.0,
        ref_min_speed_kph=150.0,
        ref_exit_speed_kph=220.0,
        direction="right",
    )
    bz_det.set_zones(0, [zone])
    spotter = Spotter()
    det = DefendWarningDetector(braking_zones=bz_det, spotter=spotter)
    state = RaceState()
    state.track_length = 5000

    # Player is at 800m (200m before braking zone at 1000m, within 300-150m window)
    # Car 1 is 8m behind (d_fwd = -8.0m)
    pkt_lap = make_lap_packet({0: {"lap_distance": 800.0, "position": 1, "lap_num": 2}, 1: {"lap_distance": 792.0, "position": 2, "lap_num": 2, "gap_ahead_ms": 300}})
    h_lap, d_lap = packets.parse(pkt_lap)
    state.update(h_lap, d_lap)

    # T=0: Car 1 behind with d_fwd=-10.0, d_rt=0.0
    det._motion_history[1] = deque([(10.0, -10.0, 0.0)])

    # T=1.0: Car 1 closes to -7.0m, d_rt stays 0.0 -> Regular Defend Warning
    pkt_mot = make_motion_packet({0: {"x": 0.0, "z": 800.0}, 1: {"x": 0.0, "z": 793.0}})
    h_mot, d_mot = packets.parse(pkt_mot)
    state.update(h_mot, d_mot)

    calls = det.update(state, now=11.0)
    assert len(calls) == 1
    assert calls[0]["corner"] == "Abbey"
    assert calls[0]["inside_look"] is False

    # Reset cooldown for inside look test
    det._last_call_time = -100.0
    det._corner_lap_fired.clear()

    # Inside look test: Corner is 'right', car 1 moves right (d_rt +1.5m > 1.0m)
    det._motion_history[1] = deque([(20.0, -8.0, 0.0)])
    pkt_mot_in = make_motion_packet({0: {"x": 0.0, "z": 800.0}, 1: {"x": 1.5, "z": 794.0}})
    h_mot_in, d_mot_in = packets.parse(pkt_mot_in)
    state.update(h_mot_in, d_mot_in)

    calls_inside = det.update(state, now=21.0)
    assert len(calls_inside) == 1
    assert calls_inside[0]["inside_look"] is True


def test_defend_warning_spotter_priority():
    """Defend warning is suppressed if spotter has an active overlap."""
    bz_det = BrakingZoneDetector()
    zone = BrakingZone(1, "Turn 1", 1000.0, 1100.0, 1000.0, 150.0, 220.0, "right")
    bz_det.set_zones(0, [zone])
    spotter = Spotter()
    spotter.state = SpotterState.CAR_LEFT  # Spotter active!
    det = DefendWarningDetector(braking_zones=bz_det, spotter=spotter)
    state = RaceState()
    state.track_length = 5000

    pkt_lap = make_lap_packet({0: {"lap_distance": 800.0, "position": 1, "lap_num": 1}, 1: {"lap_distance": 795.0, "position": 2, "lap_num": 1}})
    h_lap, d_lap = packets.parse(pkt_lap)
    state.update(h_lap, d_lap)
    pkt_mot = make_motion_packet({0: {"x": 0.0, "z": 800.0}, 1: {"x": 0.0, "z": 795.0}})
    h_mot, d_mot = packets.parse(pkt_mot)
    state.update(h_mot, d_mot)

    calls = det.update(state, now=10.0)
    assert calls == []  # Suppressed by spotter priority


# ==============================================================================
# 6. PENA Decoding Bug & Anomaly Tests
# ==============================================================================

def test_pena_decoding_and_time_anomaly():
    """PENA with time 255 (N/A) or >60s is never spoken; real penalties decoded accurately."""
    co = Callouts()
    state = RaceState()

    # 1. Corner cutting warning (time = 255) -> Track limits warning, NOT '255-second penalty'
    pkt_warn = make_pena_packet(penalty_type=5, infringement_type=0, penalty_time=255)
    h_w, d_w = packets.parse(pkt_warn)
    sigs_w = state.update(h_w, d_w)
    calls_w = co.process(sigs_w[0], state, now=10.0)
    assert len(calls_w) == 1
    assert "255" not in calls_w[0].text
    assert "penalty" not in calls_w[0].text.lower()
    assert "track limits warning" in calls_w[0].text.lower()

    # 2. Lap invalidation (time = 255) -> Lap invalidated, NOT '255-second penalty'
    pkt_inv = make_pena_packet(penalty_type=10, infringement_type=0, penalty_time=255)
    h_inv, d_inv = packets.parse(pkt_inv)
    sigs_inv = state.update(h_inv, d_inv)
    calls_inv = co.process(sigs_inv[0], state, now=20.0)
    assert len(calls_inv) == 1
    assert "255" not in calls_inv[0].text
    assert "penalty" not in calls_inv[0].text.lower()
    assert "lap invalidated" in calls_inv[0].text.lower()

    # 3. Anomaly time > 60s (e.g. 120s bug) -> Logged, not spoken
    pkt_anom = make_pena_packet(penalty_type=4, infringement_type=0, penalty_time=120)
    h_a, d_a = packets.parse(pkt_anom)
    sigs_a = state.update(h_a, d_a)
    calls_a = co.process(sigs_a[0], state, now=30.0)
    assert calls_a == []

    # 4. Real 5-second time penalty
    pkt_5s = make_pena_packet(penalty_type=4, infringement_type=0, penalty_time=5)
    h_5s, d_5s = packets.parse(pkt_5s)
    sigs_5s = state.update(h_5s, d_5s)
    calls_5s = co.process(sigs_5s[0], state, now=40.0)
    assert len(calls_5s) == 1
    assert "5-second time penalty" in calls_5s[0].text

    # 5. Drive-through penalty
    pkt_dt = make_pena_packet(penalty_type=0, infringement_type=0, penalty_time=255)
    h_dt, d_dt = packets.parse(pkt_dt)
    sigs_dt = state.update(h_dt, d_dt)
    calls_dt = co.process(sigs_dt[0], state, now=50.0)
    assert len(calls_dt) == 1
    assert "drive-through penalty" in calls_dt[0].text.lower()


def test_time_trial_never_speaks_penalties():
    """In Time Trial, PENA events never speak penalty wording; only lap invalidated once per lap."""
    co = Callouts()
    state = RaceState()
    state.time_trial = type("TT", (), {})()  # Mark as Time Trial

    # Drive-through packet -> ignored in Time Trial
    pkt_dt = make_pena_packet(penalty_type=0, infringement_type=0, penalty_time=255)
    h_dt, d_dt = packets.parse(pkt_dt)
    sigs_dt = state.update(h_dt, d_dt)
    calls_dt = co.process(sigs_dt[0], state, now=10.0)
    assert calls_dt == []

    # Lap invalidated packet -> speaks "Lap invalidated." once
    pkt_inv1 = make_pena_packet(penalty_type=10, infringement_type=0, penalty_time=255, lap_num=3)
    h_i1, d_i1 = packets.parse(pkt_inv1)
    sigs_i1 = state.update(h_i1, d_i1)
    calls_i1 = co.process(sigs_i1[0], state, now=11.0)
    assert len(calls_i1) == 1
    assert calls_i1[0].text == "Lap invalidated."

    # Second invalidation on same lap -> suppressed
    calls_i2 = co.process(sigs_i1[0], state, now=15.0)
    assert calls_i2 == []


def test_pena_and_lapdata_deduplication():
    """PENA invalidation + LapData lap_invalid within 1s are merged into one callout."""
    co = Callouts()
    state = RaceState()

    # LapData triggers lap_invalidated at T=10.0s
    calls1 = co.process(("lap_invalidated",), state, now=10.0)
    assert len(calls1) == 1
    assert calls1[0].text == "Lap invalidated."

    # PENA invalidation arrives at T=10.4s (within 1s) -> deduplicated (empty)
    ev_inv = packets.Event("PENA", 0, penalty_type=10, penalty_time=255)
    calls2 = co.process(("penalty", ev_inv), state, now=10.4)
    assert calls2 == []


# ==============================================================================
# 7. Dashboard Full State on Reconnect Tests
# ==============================================================================

def test_dashboard_full_state_on_reconnect():
    """On WebSocket connect, dashboard immediately receives full snapshot + laps + overtakes."""
    state = RaceState()
    broadcaster = Broadcaster()
    storage = SessionStorage(":memory:")

    # Inject session and overtakes
    storage.save_session({"session_uid": "9999", "game_format": 2025, "track_id": 1, "session_type": 10})
    storage.save_overtake({
        "session_uid": "9999", "lap_num": 4, "timestamp": 123.4, "other_vehicle_idx": 1,
        "other_driver_name": "Verstappen", "direction": "pass", "classification": "brave",
        "braking_delta_m": 12.5, "start_gap_m": -5.0, "overlap_at_turn_in": 1,
        "ran_deep": 0, "contact": 0, "forced_off": 0, "switchback": 0, "corner_name": "Stowe"
    })
    broadcaster.broadcast_radio("engineer", "Welcome to live session.")

    app = create_app(state, broadcaster, token="test_token", storage=storage)
    client = TestClient(app)

    with client.websocket_connect("/ws/live?token=test_token") as ws:
        frame_text = ws.receive_text()
        frame = json.loads(frame_text)
        assert frame["type"] in ("telemetry", "initial_state")
        assert "radio_history" in frame
        assert len(frame["radio_history"]) >= 1
        assert "overtakes" in frame


# ==============================================================================
# 8. Replay Explain Mode Tests
# ==============================================================================

def test_replay_explain_mode():
    """Replays a synthetic recording in explain mode and prints per-lap detected events."""
    import asyncio
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        path = f.name

    try:
        # Create synthetic race recording
        # Lap 1: Start and pass car 1
        # Lap 2: Finish
        with open(path, "wb") as f_out:
            # Packet 1: Initial state
            p1 = make_lap_packet({0: {"total_distance": 500.0, "position": 2}, 1: {"total_distance": 505.0, "position": 1}}, session_time=100.0)
            f_out.write(FRAME.pack(0.0, len(p1)) + p1)

            # Packet 2: Pass car 1
            p2 = make_lap_packet({0: {"total_distance": 600.0, "position": 1}, 1: {"total_distance": 590.0, "position": 2}}, session_time=102.5)
            f_out.write(FRAME.pack(0.1, len(p2)) + p2)

            # Packet 3: Finish P1
            p3 = make_lap_packet({0: {"total_distance": 5000.0, "position": 1, "result_status": 3, "gap_ahead_ms": 0}, 1: {"total_distance": 4980.0, "position": 2, "result_status": 3, "gap_ahead_ms": 1200}}, session_time=110.0)
            f_out.write(FRAME.pack(0.2, len(p3)) + p3)

        tracker = ExplainTracker()
        eng = Engineer()
        eng.tracker = tracker

        asyncio.run(replay(path, eng, speed=1.0, explain=True, tracker=tracker))

        assert 1 in tracker.events_by_lap
        events_lap1 = tracker.events_by_lap[1]
        assert any(e["type"] in ("OVERTAKE", "FINISH") for e in events_lap1)

    finally:
        Path(path).unlink(missing_ok=True)
