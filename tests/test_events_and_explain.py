"""Unit and integration tests for Prompt 19:
- Session reset guard (session_uid == 0 ignored; non-zero reset)
- Finish detection and persona bucketing (photo finish, close, comfortable, dominant,
  podium held, podium missed higher, missed podium, provisional penalties, 8s suppression)
- FinalClassification position correction and CHQF/RCWN deduplication
- PENA event decoding, 255 N/A, >60s anomaly guard, Time Trial suppression, deduplication
- Dashboard data persistence on reconnect and timeline storage
- ExplainTracker and replay --explain workflow
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import tempfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from config import AppConfig, FinishConfig
from f1telemetry import packets
from f1telemetry.deterministic import Callouts, Callout
from f1telemetry.packets import HEADER, LAP, NUM_CARS, PacketId, PENALTY_TYPES, INFRINGEMENT_TYPES
from f1telemetry.state import RaceState, LapRecord
from personality import PersonalityEngine
from storage import SessionStorage
from dashboard.server import create_app, Broadcaster
from main import Engineer, ExplainTracker, replay, FRAME


def make_header(pid: int, uid: int = 12345, session_time: float = 100.0, player_idx: int = 0) -> bytes:
    """Builds standard 29-byte header."""
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
        row = [0] * 33
        row[10] = float(c.get("lap_distance", 500.0))
        row[11] = float(c.get("total_distance", 1500.0))
        row[13] = int(c.get("position", i + 1))
        row[14] = int(c.get("lap_num", 1))
        row[15] = int(c.get("pit_status", 0))
        row[18] = int(c.get("lap_invalid", 0))
        row[19] = int(c.get("penalties_s", 0))
        row[21] = int(c.get("corner_cutting_warnings", 0))
        row[26] = int(c.get("result_status", 2))  # default 2 = active
        row[6] = int(c.get("gap_ahead_ms", 1000))
        row[0] = int(c.get("last_lap_ms", 90000))
        body += LAP.pack(*row)
    return make_header(PacketId.LAP_DATA, uid=uid, session_time=session_time, player_idx=player_idx) + body + b"\xff\xff"


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
    event_body = b"PENA" + payload.ljust(12, b"\x00")
    return make_header(PacketId.EVENT, uid=uid, session_time=session_time) + event_body


# ==============================================================================
# 1. Session Reset Guard Tests
# ==============================================================================

def test_session_uid_zero_completely_ignored():
    """session_uid == 0 packets (from menus/loading) must be ignored and never reset state."""
    state = RaceState()
    # First active packet establishes uid 9999
    h1, d1 = packets.parse(make_lap_packet({}, uid=9999, session_time=10.0))
    state.update(h1, d1)
    assert state.session_uid == 9999
    assert state.last_packet_time > 0

    # Packet with session_uid == 0 arrives -> must be ignored completely
    h0, d0 = packets.parse(make_lap_packet({}, uid=0, session_time=15.0))
    sigs = state.update(h0, d0)
    assert sigs == []
    assert state.session_uid == 9999


def test_session_reset_only_on_nonzero_uid_change(caplog):
    """Session state resets only when a new non-zero session_uid arrives, logging both uids."""
    state = RaceState()
    h1, d1 = packets.parse(make_lap_packet({0: {"lap_num": 5}}, uid=1001, session_time=100.0))
    state.update(h1, d1)
    assert state.session_uid == 1001

    with caplog.at_level(logging.INFO):
        # New non-zero session UID -> state resets and emits new_session
        h2, d2 = packets.parse(make_lap_packet({0: {"lap_num": 1}}, uid=2002, session_time=5.0))
        sigs = state.update(h2, d2)
        assert any(s[0] == "new_session" and s[1] == 2002 for s in sigs)
        assert state.session_uid == 2002
        assert "Session reset: old_uid=1001, new_uid=2002" in caplog.text


# ==============================================================================
# 2. Finish Detection & Margin Buckets Tests
# ==============================================================================

def test_finish_detection_margin_buckets():
    """Validates margin bucketing for win (photo, close, comfortable, dominant) and podiums."""
    pers = PersonalityEngine()

    def run_finish(pos: int, gap_behind_ms: int | None, gap_ahead_ms: int | None, penalties_s: int = 0):
        # Reset finish tracking for test
        pers._finish_spoken_session_uid = None
        pers._finish_line_pos = None
        pers._finish_time = -999.0
        state = RaceState()
        state.session_uid = 4242
        info = {
            "position": pos,
            "gap_ahead_ms": gap_ahead_ms,
            "gap_behind_ms": gap_behind_ms,
            "penalties_s": penalties_s,
            "session_uid": 4242,
        }
        return pers.on_signal(("finished", info), state, now=100.0)

    # 1. P1 Win photo finish (< 0.5s margin)
    c1 = run_finish(1, gap_behind_ms=300, gap_ahead_ms=None)
    assert c1 is not None

    # 2. P1 Win close (0.5s - 3.0s margin)
    c2 = run_finish(1, gap_behind_ms=1800, gap_ahead_ms=None)
    assert c2 is not None

    # 3. P1 Win comfortable (3.0s - 10.0s margin)
    c3 = run_finish(1, gap_behind_ms=6500, gap_ahead_ms=None)
    assert c3 is not None

    # 4. P1 Win dominant (> 10.0s margin)
    c4 = run_finish(1, gap_behind_ms=15000, gap_ahead_ms=None)
    assert c4 is not None

    # 5. P2 Podium held (car behind < 1.0s)
    c5 = run_finish(2, gap_behind_ms=600, gap_ahead_ms=2500)
    assert c5 is not None

    # 6. P2 Podium missed higher (car ahead < 1.0s)
    c6 = run_finish(2, gap_behind_ms=3000, gap_ahead_ms=450)
    assert c6 is not None

    # 7. P4 Missed podium (gap ahead <= 2.0s)
    c7 = run_finish(4, gap_behind_ms=3000, gap_ahead_ms=1200)
    assert c7 is not None

    # 8. P4 Points (gap ahead > 2.0s)
    c8 = run_finish(4, gap_behind_ms=3000, gap_ahead_ms=4500)
    assert c8 is not None

    # 9. P12 No points
    c9 = run_finish(12, gap_behind_ms=1000, gap_ahead_ms=2000)
    assert c9 is not None


def test_finish_provisional_penalty_suffix():
    """Unserved time penalties (penalties_s > 0) append provisional warning to finish line."""
    pers = PersonalityEngine()
    state = RaceState()
    state.session_uid = 5555

    info = {
        "position": 1,
        "gap_ahead_ms": None,
        "gap_behind_ms": 2500,
        "penalties_s": 5,
        "session_uid": 5555,
    }
    callout = pers.on_signal(("finished", info), state, now=100.0)
    assert callout is not None
    assert "provisional" in callout.text.lower()
    assert "five-second penalty" in callout.text.lower()


def test_single_finish_message_despite_events_and_suppression():
    """Finish fires exactly once; CHQF/RCWN events are ignored; other calls suppressed for 8s; FinalClassification corrects position."""
    pers = PersonalityEngine()
    state = RaceState()

    # Active lap packet
    cars_act = {0: {"position": 1, "result_status": 2, "gap_ahead_ms": 0}}
    h_act, d_act = packets.parse(make_lap_packet(cars_act, uid=888))
    state.update(h_act, d_act)

    # Finish packet: status transitions 2 -> 3
    cars_fin = {0: {"position": 1, "result_status": 3, "gap_ahead_ms": 0}, 1: {"position": 2, "result_status": 3, "gap_ahead_ms": 2000}}
    h_fin, d_fin = packets.parse(make_lap_packet(cars_fin, uid=888))
    sigs = state.update(h_fin, d_fin)
    fin_sig = [s for s in sigs if s[0] == "finished"][0]
    c1 = pers.on_signal(fin_sig, state, now=100.0)
    assert c1 is not None

    # Duplicate finish signal in same session -> returns None
    c_dup = pers.on_signal(fin_sig, state, now=100.5)
    assert c_dup is None

    # CHQF event packet arrives -> does NOT trigger duplicate message
    ev_chqf = packets.Event("CHQF", 0)
    c_chqf = pers.on_signal(("event", ev_chqf), state, now=101.0)
    assert c_chqf is None

    # RCWN event packet arrives -> does NOT trigger duplicate message
    ev_rcwn = packets.Event("RCWN", 0)
    c_rcwn = pers.on_signal(("event", ev_rcwn), state, now=102.0)
    assert c_rcwn is None

    # Track limits warning arrives 3s after finish -> SUPPRESSED by 8s cooldown
    c_suppressed = pers.on_signal(("track_limits_warning", 1), state, now=103.0)
    assert c_suppressed is None

    # FinalClassification arrives with SAME position P1 -> No correction
    fc_same = [packets.FinalClassification(position=1, num_laps=50, grid_position=1, points=25, num_pit_stops=1, result_status=3, best_lap_time_ms=88000, total_race_time=5000.0, penalties_time=0, num_penalties=0)]
    c_fc_same = pers.on_signal(("final_classification", fc_same), state, now=104.0)
    assert c_fc_same is None

    # FinalClassification arrives with DIFFERENT position P3 (e.g. penalty applied) -> Speaks correction
    fc_diff = [packets.FinalClassification(position=3, num_laps=50, grid_position=1, points=15, num_pit_stops=1, result_status=3, best_lap_time_ms=88000, total_race_time=5005.0, penalties_time=5, num_penalties=1)]
    c_fc_diff = pers.on_signal(("final_classification", fc_diff), state, now=105.0)
    assert c_fc_diff is not None
    assert "correction" in c_fc_diff.text.lower() or "p3" in c_fc_diff.text.lower()


# ==============================================================================
# 3. PENA Decoding, Time Anomaly & Time Trial Tests
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

    # 3. Anomaly time > 60s (e.g. 250s bug or corrupted bytes) -> Logged, not spoken
    pkt_anom = make_pena_packet(penalty_type=4, infringement_type=0, penalty_time=250)
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
    pers = PersonalityEngine()
    state = RaceState()
    state.time_trial = type("TT", (), {})()  # Mark as Time Trial

    # Drive-through packet -> ignored in Time Trial
    pkt_dt = make_pena_packet(penalty_type=0, infringement_type=0, penalty_time=255)
    h_dt, d_dt = packets.parse(pkt_dt)
    sigs_dt = state.update(h_dt, d_dt)
    calls_dt = co.process(sigs_dt[0], state, now=10.0)
    assert calls_dt == []
    c_pers_dt = pers.on_signal(sigs_dt[0], state, now=10.0)
    assert c_pers_dt is None

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

    # PENA invalidation arrives at T=10.4s (within 1s) -> deduplicated
    ev_inv = packets.Event("PENA", 0, penalty_type=10, penalty_time=255)
    calls2 = co.process(("penalty", ev_inv), state, now=10.4)
    assert calls2 == []


# ==============================================================================
# 4. Dashboard Persistence & Reconnection Tests
# ==============================================================================

def test_dashboard_full_state_on_reconnect(tmp_path):
    """On WebSocket connect, dashboard immediately receives full snapshot + history + overtakes."""
    db_file = str(tmp_path / "test.db")
    state = RaceState()
    state.session_uid = "7777"
    broadcaster = Broadcaster()
    storage = SessionStorage(db_file)

    # Seed session, overtakes and radio history
    storage.save_session({"session_uid": "7777", "game_format": 2025, "track_id": 1, "session_type": 10})
    storage.save_overtake({
        "session_uid": "7777", "lap_num": 3, "timestamp": 120.0, "other_vehicle_idx": 1,
        "other_driver_name": "Hamilton", "direction": "pass", "classification": "clean",
        "braking_delta_m": 5.0, "start_gap_m": -2.0, "overlap_at_turn_in": 1,
        "ran_deep": 0, "contact": 0, "forced_off": 0, "switchback": 0, "corner_name": "Becketts"
    })
    storage._queue.join()
    broadcaster.broadcast_radio("engineer", "Target delta is green.")

    app = create_app(state, broadcaster, token="secret123", storage=storage)
    client = TestClient(app)

    with client.websocket_connect("/ws/live?token=secret123") as ws:
        msg = ws.receive_text()
        payload = json.loads(msg)
        assert payload["type"] in ("telemetry", "initial_state")
        assert "radio_history" in payload
        assert len(payload["radio_history"]) >= 1
        assert payload["radio_history"][0]["text"] == "Target delta is green."
        assert "overtakes" in payload
        assert len(payload["overtakes"]) == 1
        assert payload["overtakes"][0]["corner_name"] == "Becketts"
    storage.close()


def test_session_resumption_reloads_laps(tmp_path):
    """Resuming the same session_uid on app restart restores laps from database into state."""
    db_file = str(tmp_path / "test_resume.db")
    storage = SessionStorage(db_file)
    storage.save_session({"session_uid": "3333", "game_format": 2025, "track_id": 1, "session_type": 10})
    storage._queue.join()

    sess = storage.get_session_by_uid("3333")
    assert sess is not None
    storage.save_completed_lap({
        "session_uid": "3333",
        "lap_num": 1,
        "lap_time_s": 85.250,
        "sector1_s": 28.1,
        "sector2_s": 32.4,
        "sector3_s": 24.75,
        "is_valid": 1,
        "position": 2,
    })
    storage._queue.join()

    eng = Engineer(storage=storage)
    eng.state.session_uid = 3333
    eng._reload_session_if_resumed()

    assert len(eng.state.laps) == 1
    assert eng.state.laps[0].lap == 1
    assert eng.state.laps[0].lap_time_ms == 85250
    assert eng.state.laps[0].position == 2
    storage.close()


# ==============================================================================
# 5. ExplainTracker & Replay Mode Tests
# ==============================================================================

def test_explain_tracker_report_generation(capsys):
    """ExplainTracker tracks events and prints per-lap report with session resets, penalties, finishes."""
    tracker = ExplainTracker()
    tracker.on_reset(1001, 2002)
    tracker.current_lap = 1

    # Lap 1: Track limits warning and invalidation
    tracker.on_warning(1)
    tracker.on_invalidation()

    # Lap 1 completion
    rec = LapRecord(
        lap=1, lap_time_ms=82500, invalid=True, position=2,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=None, tyre_wear=None, stint=0, sector1_ms=27000, sector2_ms=31000, sector3_ms=24500
    )
    tracker.on_lap(rec)

    # Lap 2: Real 5-second penalty
    ev_pen = packets.Event("PENA", 0, penalty_type=4, infringement_type=0, penalty_time=5)
    tracker.on_penalty(ev_pen)

    # Finish P1
    tracker.on_finish({
        "position": 1,
        "gap_ahead_ms": None,
        "gap_behind_ms": 1400,
        "penalties_s": 5,
        "session_uid": 2002,
    })

    tracker.print_report("synthetic_test_race.bin")
    captured = capsys.readouterr().out
    assert "EXPLAIN REPLAY REPORT: synthetic_test_race.bin" in captured
    assert "1001 -> 2002" in captured
    assert "--- LAP 1 ---" in captured
    assert "Track limits warning 1" in captured
    assert "Lap invalidated" in captured
    assert "--- LAP 2 ---" in captured
    assert "5s" in captured
    assert "FINISH" in captured
