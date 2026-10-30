"""Unit tests for Prompt 23: Live track map binary streaming, interpolation, extrapolation, fallback, and replay."""
import asyncio
import math
import struct
import time
from pathlib import Path

import pytest

from f1telemetry import packets
from f1telemetry.packets import HEADER, LAP, NUM_CARS, PacketId, Session
from f1telemetry.state import RaceState
from positions import (
    CAR_STRUCT,
    FLAG_DRS_ACTIVE,
    FLAG_ESTIMATED,
    FLAG_OUT_LAP,
    FLAG_PIT,
    FLAG_PLAYER,
    FLAG_RETIRED,
    FLAG_WRONG_WAY,
    HEADER_STRUCT,
    CarPosition,
    PositionsBroadcaster,
    decode_positions_frame,
    encode_positions_frame,
    extrapolate_position,
    extract_car_positions,
    interpolate_positions,
)
from replay_manager import FRAME, Keyframe, ReplayController, ReplayMarker
from trackmap import TrackOutline
from trackmodel import BinData, TrackModel


def test_binary_frame_round_trip():
    """Verify compact binary frame encodes 24 cars into exactly 370 bytes and decodes with precision."""
    assert HEADER_STRUCT.size == 10
    assert CAR_STRUCT.size == 15
    expected_total_bytes = 10 + 24 * 15
    assert expected_total_bytes == 370

    cars = []
    for i in range(24):
        x = -1234.5 + i * 100.2
        z = 5678.9 - i * 50.4
        heading = (i * 0.25) % (2 * math.pi)
        speed = 50.0 + i * 10.0
        pos = i + 1
        flags = 0
        if i == 0:
            flags |= FLAG_PLAYER
        if i == 3:
            flags |= FLAG_PIT
        if i == 7:
            flags |= FLAG_RETIRED
        if i == 11:
            flags |= FLAG_OUT_LAP
        if i == 15:
            flags |= FLAG_WRONG_WAY
        if i == 19:
            flags |= FLAG_ESTIMATED
        if i == 23:
            flags |= FLAG_DRS_ACTIVE

        cars.append(CarPosition(car_idx=i, x=x, z=z, heading=heading, speed_kph=speed, position=pos, flags=flags))

    # Encode
    frame_bytes = encode_positions_frame(seq=1234, timestamp_ms=9876543, track_id=7, cars=cars)
    assert len(frame_bytes) == 370

    # Decode
    decoded = decode_positions_frame(frame_bytes)
    assert decoded["seq"] == 1234
    assert decoded["timestamp_ms"] == 9876543
    assert decoded["track_id"] == 7
    assert decoded["num_cars"] == 24

    for i, dec in enumerate(decoded["cars"]):
        orig = cars[i]
        assert dec.car_idx == orig.car_idx
        assert math.isclose(dec.x, orig.x, abs_tol=0.1)
        assert math.isclose(dec.z, orig.z, abs_tol=0.1)
        assert math.isclose(dec.heading, orig.heading, abs_tol=0.01)
        assert dec.speed_kph == int(orig.speed_kph)
        assert dec.position == orig.position
        assert dec.flags == orig.flags

        # Test flags property getters
        if i == 0:
            assert dec.is_player
        if i == 3:
            assert dec.is_pit
        if i == 7:
            assert dec.is_retired
        if i == 11:
            assert dec.is_out_lap
        if i == 15:
            assert dec.is_wrong_way
        if i == 19:
            assert dec.is_estimated
        if i == 23:
            assert dec.is_drs_active


def test_interpolation_and_extrapolation_timing():
    """Verify linear interpolation, shortest-angle heading wrapping, and dead reckoning extrapolation holding after 200ms."""
    # 1. Linear interpolation
    p0 = CarPosition(car_idx=1, x=10.0, z=20.0, heading=0.2, speed_kph=100.0, position=2)
    p1 = CarPosition(car_idx=1, x=30.0, z=60.0, heading=0.6, speed_kph=200.0, position=2)

    mid = interpolate_positions(p0, p1, 0.5)
    assert math.isclose(mid.x, 20.0, abs_tol=1e-3)
    assert math.isclose(mid.z, 40.0, abs_tol=1e-3)
    assert math.isclose(mid.speed_kph, 150.0, abs_tol=1e-3)
    assert math.isclose(mid.heading, 0.4, abs_tol=1e-3)

    # 2. Shortest-angle heading interpolation wrapping around 2*pi
    # Angle 0.1 rad (just above 0) and 6.20 rad (just below 2*pi = 6.283)
    # The shortest path crosses 0 rad (total angular distance = 0.1 + (2*pi - 6.20) ~= 0.183 rad)
    # At t=0.5, heading should be near 0.0 or 2*pi, NOT around 3.15!
    p_start = CarPosition(car_idx=1, x=0.0, z=0.0, heading=0.05, speed_kph=100.0, position=1)
    p_end = CarPosition(car_idx=1, x=0.0, z=0.0, heading=6.25, speed_kph=100.0, position=1)
    p_mid = interpolate_positions(p_start, p_end, 0.5)
    # Expected mid is around 0.015 or 6.27
    assert p_mid.heading < 0.2 or p_mid.heading > 6.1

    # 3. Extrapolation with velocity
    # Heading 0 -> points in +Z direction. Speed 36 km/h = 10 m/s
    pos = CarPosition(car_idx=0, x=100.0, z=200.0, heading=0.0, speed_kph=36.0, position=1)

    # dt = 0.100s (100 ms) -> advances 1.0 m in Z
    e100 = extrapolate_position(pos, dt_s=0.100)
    assert math.isclose(e100.x, 100.0, abs_tol=1e-3)
    assert math.isclose(e100.z, 201.0, abs_tol=1e-3)

    # dt = 0.200s (200 ms) -> advances 2.0 m in Z
    e200 = extrapolate_position(pos, dt_s=0.200)
    assert math.isclose(e200.z, 202.0, abs_tol=1e-3)

    # dt = 0.350s (> 200 ms) -> must cap at 200ms and hold!
    e350 = extrapolate_position(pos, dt_s=0.350)
    assert math.isclose(e350.z, 202.0, abs_tol=1e-3)  # Holds position, does not drift beyond 200ms


def test_fallback_placement_from_lap_distance():
    """When Motion packet is absent (spectating, garage), cars are placed on track model centreline flagged as estimated."""
    state = RaceState(ptt_action=1)

    # Create synthetic track model with 1500 2-metre bins (3000m track)
    bins = []
    for idx in range(1500):
        dist = idx * 2.0
        x = 100.0 + dist * 0.25
        z = 200.0 + dist * 0.40
        bins.append(BinData(bin_index=idx, lap_distance_m=dist, x=x, y=0.0, z=z, heading=1.2, curvature=0.0, left_edge_m=-5.0, right_edge_m=5.0, median_speed_kph=220.0))
    track_model = TrackModel(track_id=7, track_length_m=3000.0, bins=bins, is_ready=True)

    # Inject LapData using test_synthetic.lap_packet
    from test_synthetic import lap_packet
    lap_raw = bytearray(lap_packet(lap=1, cars={0: (1, 0, 90000), 1: (2, 500, 90500)}))
    f0 = list(LAP.unpack_from(lap_raw, HEADER.size))
    f0[10] = 1000.0  # m_lapDistance
    LAP.pack_into(lap_raw, HEADER.size, *f0)

    f1 = list(LAP.unpack_from(lap_raw, HEADER.size + LAP.size))
    f1[10] = 2000.0  # m_lapDistance
    LAP.pack_into(lap_raw, HEADER.size + LAP.size, *f1)

    h, data = packets.parse(bytes(lap_raw))
    state.update(h, data)
    assert state.motion is None  # Motion has not arrived

    # Extract positions with track_model fallback
    positions = extract_car_positions(state, track_model=track_model)
    assert len(positions) >= 2

    p0 = [p for p in positions if p.car_idx == 0][0]
    assert p0.is_estimated
    assert math.isclose(p0.x, 350.0, abs_tol=1.0)
    assert math.isclose(p0.z, 600.0, abs_tol=1.0)
    assert math.isclose(p0.heading, 1.2, abs_tol=0.1)

    p1 = [p for p in positions if p.car_idx == 1][0]
    assert p1.is_estimated
    assert math.isclose(p1.x, 600.0, abs_tol=1.0)
    assert math.isclose(p1.z, 1000.0, abs_tol=1.0)


@pytest.mark.anyio
async def test_frame_dropping_for_slow_client():
    """Slow client queue size 1 drops old frames without blocking the broadcaster."""
    broadcaster = PositionsBroadcaster()
    q = broadcaster.register()
    assert broadcaster.client_count == 1

    f1 = b"FRAME_1"
    f2 = b"FRAME_2"
    f3 = b"FRAME_3"

    # Send 3 frames while client is not reading
    broadcaster.broadcast_frame(f1)
    assert q.full()

    broadcaster.broadcast_frame(f2)
    assert q.full()
    assert broadcaster.dropped_frames >= 1

    broadcaster.broadcast_frame(f3)
    assert q.full()
    assert broadcaster.dropped_frames >= 2

    # Client reads from queue: should receive latest frame f3 immediately
    received = q.get_nowait()
    assert received == f3
    assert q.empty()

    broadcaster.unregister(q)
    assert broadcaster.client_count == 0


def test_replay_seek_accuracy_and_markers(tmp_path: Path):
    """Replay builds keyframes, extracts markers, and seeks accurately to timestamps."""
    rec_file = tmp_path / "test_session.bin"

    from test_synthetic import event, header, lap_packet, session_packet

    h_sess = session_packet()
    h_lap = lap_packet(lap=1, cars={0: (1, 0, 90000), 1: (2, 500, 90500)})
    h_otke = event("OTKE", bytes([0, 1]))  # car 0 overtook car 1
    
    # Session with SC=1
    sc_buf = bytearray(session_packet())
    sc_buf[153] = 1
    h_sc = bytes(sc_buf)

    # Lap data with car 0 in pit (row[15] is pitStatus in LAP)
    pit_lap = bytearray(h_lap)
    fields = list(LAP.unpack_from(pit_lap, HEADER.size))
    fields[15] = 1  # pitStatus = 1
    LAP.pack_into(pit_lap, HEADER.size, *fields)
    h_pit = bytes(pit_lap)

    with open(rec_file, "wb") as f:
        f.write(FRAME.pack(0.0, len(h_sess)) + h_sess)
        f.write(FRAME.pack(2.0, len(h_lap)) + h_lap)
        f.write(FRAME.pack(4.0, len(h_otke)) + h_otke)
        f.write(FRAME.pack(6.0, len(h_sc)) + h_sc)
        f.write(FRAME.pack(8.0, len(h_pit)) + h_pit)

    controller = ReplayController()
    status = controller.load_recording(rec_file)
    assert status["loaded"] is True
    assert status["duration_s"] >= 8.0
    assert len(controller.session.keyframes) >= 4

    # Check detected markers
    markers = status["markers"]
    marker_types = [m["marker_type"] for m in markers]
    assert "overtake" in marker_types
    assert "sc" in marker_types
    assert "pit" in marker_types

    # Seek accuracy: seek to t=4.5
    controller.seek(4.5)
    assert math.isclose(controller.current_time_s, 4.5, abs_tol=1e-2)

    # Seek to t=7.0
    controller.seek(7.0)
    assert math.isclose(controller.current_time_s, 7.0, abs_tol=1e-2)

    controller.session.close()


def test_replay_api_and_positions_websocket(tmp_path: Path):
    """Test dashboard /api/replay/* endpoints and /ws/positions binary WebSocket."""
    from fastapi.testclient import TestClient
    from dashboard.server import create_app
    from test_synthetic import lap_packet, session_packet

    # Create dummy recording
    rec_file = tmp_path / "api_test_session.bin"
    h_sess = session_packet()
    h_lap = lap_packet(lap=1, cars={0: (1, 0, 90000), 1: (2, 500, 90500)})
    with open(rec_file, "wb") as f:
        f.write(FRAME.pack(0.0, len(h_sess)) + h_sess)
        f.write(FRAME.pack(3.0, len(h_lap)) + h_lap)

    from dashboard.server import Broadcaster
    state = RaceState()
    bcast = Broadcaster()
    broadcaster = PositionsBroadcaster()
    replay_ctrl = ReplayController(broadcaster=broadcaster)
    app = create_app(
        state=state,
        broadcaster=bcast,
        token="test_token",
        positions_broadcaster=broadcaster,
        replay_controller=replay_ctrl,
    )

    client = TestClient(app)

    # 1. List sessions
    res = client.get("/api/replay/sessions?token=test_token")
    assert res.status_code == 200
    assert isinstance(res.json(), list)

    # 2. Load session
    res = client.post("/api/replay/load?token=test_token", json={"path": str(rec_file)})
    assert res.status_code == 200
    data = res.json()
    assert data["loaded"] is True
    assert data["duration_s"] >= 3.0

    # 3. Status
    res = client.get("/api/replay/status?token=test_token")
    assert res.status_code == 200
    assert res.json()["loaded"] is True

    # 4. Play, Pause, Seek
    res = client.post("/api/replay/play?token=test_token", json={"speed": 2.0})
    assert res.status_code == 200
    assert res.json()["playing"] is True
    assert res.json()["speed"] == 2.0

    res = client.post("/api/replay/pause?token=test_token")
    assert res.status_code == 200
    assert res.json()["playing"] is False

    res = client.post("/api/replay/seek?token=test_token", json={"time_s": 2.5})
    assert res.status_code == 200
    assert math.isclose(res.json()["current_time_s"], 2.5, abs_tol=1e-2)

    # 5. Connect to /ws/positions and receive binary frame
    with client.websocket_connect("/ws/positions?token=test_token") as ws:
        # Broadcast a frame
        frame_bytes = encode_positions_frame(
            seq=42, timestamp_ms=1000, track_id=5,
            cars=[CarPosition(0, 10.0, 20.0, 1.0, 200.0, 1, FLAG_PLAYER)]
        )
        broadcaster.broadcast_frame(frame_bytes)
        received = ws.receive_bytes()
        assert len(received) == 25  # 10 header + 15 for 1 car
        assert received[:2] == b"FP"
        decoded = decode_positions_frame(received)
        assert decoded["seq"] == 42
        assert decoded["num_cars"] == 1
        assert decoded["cars"][0].is_player is True

    if replay_ctrl.session:
        replay_ctrl.session.close()

