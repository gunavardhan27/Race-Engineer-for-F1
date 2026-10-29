"""Tests for deterministic live spotter, automatic end-of-lap debrief, and track limits/penalties."""
import struct
import time
import pytest

from config import DebriefConfig, SpotterConfig
from f1telemetry.deterministic import Callouts, Callout
from f1telemetry.packets import CarMotion, Header, LapData, PacketId, parse_event, parse_lap_data, parse_motion
from f1telemetry.spotter import Spotter, SpotterState
from f1telemetry.state import RaceState


def test_spotter_y_filter_crossover():
    """Cars with |ΔY| > 3.0 m (Suzuka crossover/bridge) must be ignored."""
    spotter = Spotter(SpotterConfig(lateral_distance_m=3.6, longitudinal_distance_m=4.8))
    st = RaceState()
    st.player = 0

    # Player at (0, 0, 0) facing +Z (forward = (0, 0, 1), right = (1, 0, 0))
    p_mot = CarMotion(x=0.0, y=0.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0,
                      fwd_x=0.0, fwd_y=0.0, fwd_z=1.0, right_x=1.0, right_y=0.0, right_z=0.0)
    # Opponent laterally 2.0m to the right, longitudinally aligned, but 4.0m above on a bridge
    opp_mot_bridge = CarMotion(x=2.0, y=4.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0)

    p_lap = LapData(last_lap_ms=80000, current_lap_ms=10000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                    lap_distance=500.0, position=1, lap_num=2, pit_status=0, num_pit_stops=0, sector=0,
                    lap_invalid=False, penalties_s=0, result_status=2)
    opp_lap = LapData(last_lap_ms=80000, current_lap_ms=10000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                      lap_distance=500.0, position=2, lap_num=2, pit_status=0, num_pit_stops=0, sector=0,
                      lap_invalid=False, penalties_s=0, result_status=2)

    # Set telemetry speed above min_speed_kph (20 kph)
    class FakeTel:
        speed_kph = 180

    st.tel = [FakeTel(), FakeTel()]
    st.motion = [p_mot, opp_mot_bridge]
    st.lap = [p_lap, opp_lap]

    # Should ignore because |ΔY| = 4.0 > 3.0
    callout = spotter.process(st, now=10.0)
    assert callout is None
    assert spotter.state == SpotterState.CLEAR

    # Lower opponent to same level (ΔY = 0.5 <= 3.0)
    opp_mot_level = CarMotion(x=2.0, y=0.5, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0)
    st.motion = [p_mot, opp_mot_level]

    callout2 = spotter.process(st, now=10.1)
    assert callout2 == "Car right"
    assert spotter.state == SpotterState.CAR_RIGHT


def test_spotter_direct_side_switch():
    """Direct CAR_LEFT <-> CAR_RIGHT transitions must announce the new side."""
    spotter = Spotter(SpotterConfig(lateral_distance_m=3.6, longitudinal_distance_m=4.8))
    st = RaceState()
    st.player = 0

    class FakeTel:
        speed_kph = 150

    p_mot = CarMotion(x=0.0, y=0.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0,
                      fwd_x=0.0, fwd_y=0.0, fwd_z=1.0, right_x=1.0, right_y=0.0, right_z=0.0)
    # Start on left (-2.0 m)
    opp_left = CarMotion(x=-2.0, y=0.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0)

    p_lap = LapData(last_lap_ms=80000, current_lap_ms=10000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                    lap_distance=500.0, position=1, lap_num=2, pit_status=0, num_pit_stops=0, sector=0,
                    lap_invalid=False, penalties_s=0, result_status=2)
    opp_lap = LapData(last_lap_ms=80000, current_lap_ms=10000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                      lap_distance=500.0, position=2, lap_num=2, pit_status=0, num_pit_stops=0, sector=0,
                      lap_invalid=False, penalties_s=0, result_status=2)

    st.tel = [FakeTel(), FakeTel()]
    st.motion = [p_mot, opp_left]
    st.lap = [p_lap, opp_lap]

    # Initial left
    assert spotter.process(st, now=1.0) == "Car left"
    assert spotter.state == SpotterState.CAR_LEFT

    # Switch directly to right (+2.0 m)
    opp_right = CarMotion(x=2.0, y=0.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0)
    st.motion = [p_mot, opp_right]

    assert spotter.process(st, now=1.5) == "Car right"
    assert spotter.state == SpotterState.CAR_RIGHT

    # Switch back directly to left (-2.0 m)
    st.motion = [p_mot, opp_left]
    assert spotter.process(st, now=2.0) == "Car left"
    assert spotter.state == SpotterState.CAR_LEFT


def test_merged_warning_and_invalidation():
    """If warning and invalidation occur within 1 s, speak one merged message."""
    cfg = DebriefConfig(track_limits_penalty_at=3)
    callouts = Callouts(debrief_config=cfg)
    st = RaceState()

    t0 = 100.0
    # 1. Warning arrives first at t0
    c1 = callouts.process(("track_limits_warning", 2), st)
    assert len(c1) == 1
    assert c1[0].text == "Track limits warning, 2 of 3."

    # 2. Invalidation arrives 0.4s later (within 1s)
    # Mock time
    orig_time = time.monotonic
    try:
        callouts._last_warning_time = t0
        c2 = callouts._on_lap_invalidated(now=t0 + 0.4)
        assert len(c2) == 1
        assert c2[0].text == "Track limits, lap invalidated. Warning 2 of 3."
    finally:
        pass


def test_counter_decrease_after_flashback():
    """Flashback or penalty served decreasing counters must reset baseline silently."""
    st = RaceState()
    h = Header(2025, 2025, 1, PacketId.LAP_DATA, 12345, 10.0, 100, 0)

    # Initial lap data with 2 warnings
    lap1 = LapData(last_lap_ms=80000, current_lap_ms=10000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                   lap_distance=500.0, position=1, lap_num=5, pit_status=0, num_pit_stops=0, sector=0,
                   lap_invalid=False, penalties_s=0, result_status=2, total_warnings=2, corner_cutting_warnings=2)

    sigs1 = st.update(h, [lap1])
    assert st._corner_cutting_warnings == 2

    # Flashback occurs: lap number rewinds to 4, warnings rewind to 1
    lap_fb = LapData(last_lap_ms=80000, current_lap_ms=5000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                     lap_distance=200.0, position=1, lap_num=4, pit_status=0, num_pit_stops=0, sector=0,
                     lap_invalid=False, penalties_s=0, result_status=2, total_warnings=1, corner_cutting_warnings=1)

    sigs2 = st.update(h, [lap_fb])
    # Must emit flashback signal, but NOT track_limits_warning
    sig_names = [s[0] for s in sigs2]
    assert "flashback" in sig_names
    assert "track_limits_warning" not in sig_names
    assert st._corner_cutting_warnings == 1


def test_pena_decoding():
    """Verify PENA event decoding and callouts for time penalty, drive-through and stop-go."""
    # HEADER (29 bytes) + 4-byte code "PENA" + 7 bytes (penaltyType, infringementType, vehicleIdx, otherVehicleIdx, time, lapNum, placesGained)
    header_bytes = struct.pack("<HBBBBBQfIIBB", 2025, 25, 1, 0, 1, PacketId.EVENT, 999, 1.0, 1, 0, 0, 0)

    # 1. Drive-through penalty (type 0)
    pena_dt = header_bytes + b"PENA" + struct.pack("<BBBBBBB", 0, 1, 0, 255, 0, 5, 0)
    ev_dt = parse_event(pena_dt, Header(2025, 2025, 1, PacketId.EVENT, 999, 1.0, 1, 0))
    assert ev_dt.code == "PENA"
    assert ev_dt.penalty_type == 0
    assert ev_dt.vehicle_idx == 0

    callouts = Callouts()
    res_dt = callouts.process(("penalty", ev_dt), RaceState())
    assert len(res_dt) == 1
    assert res_dt[0].text == "Drive-through penalty."

    # 2. Stop-and-go penalty (type 1, 10s)
    pena_sg = header_bytes + b"PENA" + struct.pack("<BBBBBBB", 1, 2, 0, 255, 10, 5, 0)
    ev_sg = parse_event(pena_sg, Header(2025, 2025, 1, PacketId.EVENT, 999, 1.0, 1, 0))
    assert ev_sg.penalty_type == 1
    assert ev_sg.penalty_time == 10

    res_sg = callouts.process(("penalty", ev_sg), RaceState())
    assert len(res_sg) == 1
    assert res_sg[0].text == "10-second stop-and-go penalty."

    # 3. 5-second time penalty (type 4, 5s)
    pena_tp = header_bytes + b"PENA" + struct.pack("<BBBBBBB", 4, 3, 0, 255, 5, 5, 0)
    ev_tp = parse_event(pena_tp, Header(2025, 2025, 1, PacketId.EVENT, 999, 1.0, 1, 0))
    assert ev_tp.penalty_type == 4
    assert ev_tp.penalty_time == 5

    res_tp = callouts.process(("penalty", ev_tp), RaceState())
    assert len(res_tp) == 1
    assert res_tp[0].text == "5-second time penalty."


def test_debrief_release_timing():
    """Debrief is queued and only released when speed > 200 and |steer| < 0.1 for 1s, or after 15s."""
    callouts = Callouts(DebriefConfig())
    st = RaceState()
    st.player = 0

    class FakeTel:
        def __init__(self, speed, steer):
            self.speed_kph = speed
            self.steer = steer

    # Queue debrief at t=0
    callouts.queue_debrief("1:28.4, plus 0.3 to your best.", lap_num=3, now=0.0)
    assert callouts.pending_debrief is not None

    # At t=2.0: in corner (speed = 140 kph, steer = 0.5) -> should NOT release
    st.tel = [FakeTel(speed=140, steer=0.5)]
    assert callouts.check_debrief_release(st, now=2.0) == []

    # At t=3.0: driver enters straight (speed = 210 kph, steer = 0.02)
    st.tel = [FakeTel(speed=210, steer=0.02)]
    # Check immediately -> straight_start_time set, but not yet 1s -> should NOT release
    assert callouts.check_debrief_release(st, now=3.0) == []

    # At t=3.5: still on straight (0.5s elapsed) -> should NOT release
    assert callouts.check_debrief_release(st, now=3.5) == []

    # At t=4.1: on straight for 1.1s (>= 1.0s) -> MUST release debrief!
    rel = callouts.check_debrief_release(st, now=4.1)
    assert len(rel) == 1
    assert rel[0].text == "1:28.4, plus 0.3 to your best."
    assert callouts.pending_debrief is None

    # Test timeout release (15s):
    callouts.queue_debrief("1:29.0, lap was deleted.", lap_num=4, now=10.0)
    # Slow in pits / corners for 15s
    st.tel = [FakeTel(speed=80, steer=0.3)]
    assert callouts.check_debrief_release(st, now=20.0) == []
    # At t=25.1s (15.1s elapsed) -> MUST release due to timeout!
    rel_timeout = callouts.check_debrief_release(st, now=25.1)
    assert len(rel_timeout) == 1
    assert rel_timeout[0].text == "1:29.0, lap was deleted."

    # Test newer lap completes before release -> drops older debrief
    callouts.queue_debrief("Old lap debrief", lap_num=5, now=30.0)
    from f1telemetry.state import LapRecord
    rec_new = LapRecord(lap=6, lap_time_ms=85000, invalid=False, position=1,
                        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
                        fuel_kg=None, tyre_wear=None, stint=0)
    # Processing new lap clears pending_debrief
    callouts.process(("lap", rec_new), st)
    assert callouts.pending_debrief is None
