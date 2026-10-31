"""Tests for live web dashboard: payload builder, change detection, broadcaster, and token authentication."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dashboard.server import (
    Broadcaster,
    build_live_payload,
    create_app,
    has_changed,
    verify_token,
)
from f1telemetry.state import RaceState
from test_synthetic import (
    damage_packet,
    lap_packet,
    motion_packet,
    participants_packet,
    session_packet,
    status_packet,
    telemetry_packet,
)
from trackmap import TrackMapManager


def _populate_state() -> RaceState:
    state = RaceState(ptt_action=1)
    # Feed synthetic packets
    from f1telemetry import packets
    for raw in [
        participants_packet({0: "Player", 1: "LECLERC", 2: "NORRIS"}),
        session_packet(),
        motion_packet({0: (120.0, 340.0, 1.57, 1.8, -2.5, 1.0), 1: (150.0, 360.0, 1.50, 0.0, 0.0, 1.0)}),
        telemetry_packet(),
        status_packet(40.2, -0.3),
        damage_packet((25, 26, 40, 38), 5),
        lap_packet(6, {1: (1, 0, 90500), 0: (2, 1100, 91234), 2: (3, 900, 92000)}),
    ]:
        h, data = packets.parse(raw)
        state.update(h, data)
    return state


def test_payload_builder(tmp_path):
    """Live payload builder must include snapshot fields, extra telemetry, timing tower, motion, and car positions."""
    state = _populate_state()
    from trackmap import TrackMapStorage
    storage = TrackMapStorage(str(tmp_path / "tracks.db"))
    tm = TrackMapManager(storage)
    payload = build_live_payload(state, track_map=tm)

    # Base snapshot fields
    assert payload["track"] == "Silverstone"
    assert payload["lap"] == 6
    assert payload["position"] == 2
    assert payload["total_laps"] == 30
    assert payload["last_lap_s"] == 91.23

    # Extra telemetry fields
    assert payload["type"] == "telemetry"
    assert payload["speed_kph"] == 250
    assert payload["gear"] == 7
    assert payload["rpm"] == 11000
    assert payload["last_packet_time"] > 0

    # Motion & G-Force
    assert payload["g_force"] == {"lat": 1.8, "lon": -2.5, "vert": 1.0}

    # Car positions for track map
    assert payload["has_track_outline"] is False
    assert payload["track_id"] == 7
    cars = payload["car_positions"]
    assert isinstance(cars, list)
    assert len(cars) >= 2
    player_car = next(c for c in cars if c["is_player"])
    assert player_car["idx"] == 0
    assert player_car["x"] == 120.0
    assert player_car["z"] == 340.0
    assert player_car["yaw"] == 1.57
    assert player_car["position"] == 2

    # Timing tower view
    tower = payload.get("timing_tower")
    assert isinstance(tower, list)
    assert len(tower) == 3

    # Must be ordered by position: P1 Leclerc, P2 Player, P3 Norris
    assert tower[0]["position"] == 1
    assert tower[0]["name"] == "Leclerc"
    assert tower[0]["is_player"] is False
    assert tower[0]["gap_to_leader"] == 0.0 or tower[0]["gap_to_leader"] is not None

    assert tower[1]["position"] == 2
    assert tower[1]["name"] == "Player"
    assert tower[1]["is_player"] is True
    assert tower[1]["gap_to_ahead"] == 1.1

    assert tower[2]["position"] == 3
    assert tower[2]["name"] == "Norris"
    assert tower[2]["is_player"] is False
    assert tower[2]["gap_to_ahead"] == 0.9

    # Each car has compound, tyre age, pit stops, penalties
    for car in tower:
        assert "tyre_compound" in car
        assert "tyre_age" in car
        assert "pit_stops" in car
        assert "penalties" in car


def test_change_detection():
    """Payloads should only broadcast when telemetry values change."""
    state = _populate_state()
    p1 = build_live_payload(state)

    # None comparison
    assert has_changed(p1, None) is True
    # Identical comparison
    assert has_changed(p1, p1) is False

    # Deep copy with identical values
    p1_copy = json.loads(json.dumps(p1))
    assert has_changed(p1_copy, p1) is False

    # Modified payload (e.g. position change or gap change)
    p2 = json.loads(json.dumps(p1))
    p2["position"] = 1
    p2["car_ahead"] = None
    assert has_changed(p2, p1) is True

    # Broadcaster change detection
    broadcaster = Broadcaster()
    q = broadcaster.register()
    # Drain any initial frame
    while not q.empty():
        q.get_nowait()

    # First broadcast: changed -> True
    assert broadcaster.broadcast_state(p1) is True
    assert not q.empty()
    frame1 = q.get_nowait()
    assert json.loads(frame1)["position"] == 2

    # Second broadcast with identical payload: unchanged -> False
    assert broadcaster.broadcast_state(p1) is False
    assert q.empty()

    # Third broadcast with updated payload: changed -> True
    assert broadcaster.broadcast_state(p2) is True
    assert not q.empty()
    frame2 = q.get_nowait()
    assert json.loads(frame2)["position"] == 1


def test_broadcaster_slow_and_fast_clients():
    """One broadcaster with one fast client and one slow client.
    
    A slow client must never block the broadcaster or the fast client,
    and old frames must be dropped (queue size 1).
    """
    broadcaster = Broadcaster()
    fast_q = broadcaster.register()
    slow_q = broadcaster.register()

    # Drain initial frames
    for q in (fast_q, slow_q):
        while not q.empty():
            q.get_nowait()

    fast_received = []

    # Broadcaster pushes 10 consecutive frames
    for i in range(10):
        frame = {"frame_id": i, "data": f"packet_{i}"}
        broadcaster.push_message(frame)

        # Fast client consumes frame immediately as it arrives
        if not fast_q.empty():
            fast_received.append(json.loads(fast_q.get_nowait()))

        # Slow client DOES NOT consume (simulating lagging websocket client)

    # Fast client received all 10 frames
    assert len(fast_received) == 10
    assert [f["frame_id"] for f in fast_received] == list(range(10))

    # Slow client queue was never blocked and has maxsize 1
    assert slow_q.qsize() == 1
    slow_last_frame = json.loads(slow_q.get_nowait())
    # Older frames were dropped; slow client only gets the freshest frame (frame_id 9)
    assert slow_last_frame["frame_id"] == 9
    assert slow_q.empty()


def test_token_authentication():
    """Shared-secret access must accept valid token and reject invalid/missing tokens."""
    token = "simrig_secret_token_123"

    # Unit verification check
    assert verify_token(token, token) is True
    assert verify_token("wrong_token", token) is False
    assert verify_token(None, token) is False
    assert verify_token("", token) is False

    # HTTP & WebSocket endpoints check
    state = RaceState(ptt_action=1)
    broadcaster = Broadcaster()
    app = create_app(state, broadcaster, token)
    client = TestClient(app)

    # 1. HTTP GET / without token -> 403 Forbidden
    resp = client.get("/")
    assert resp.status_code == 403
    assert "Access Denied" in resp.text

    # 2. HTTP GET / with invalid token -> 403 Forbidden
    resp = client.get("/?token=invalid_key")
    assert resp.status_code == 403

    # 3. HTTP GET / with valid token -> 200 OK and index.html
    resp = client.get(f"/?token={token}")
    assert resp.status_code == 200
    assert "F1 25 Race Engineer Live Dashboard" in resp.text

    # 4. WebSocket /ws/live without token -> 1008 Policy Violation / Rejected
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/live"):
            pass
    assert exc_info.value.code == 1008

    # 5. WebSocket /ws/live with wrong token -> 1008 Policy Violation
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/live?token=bad_token"):
            pass
    assert exc_info.value.code == 1008

    # 6. WebSocket /ws/live with valid token -> Accepted & receives initial snapshot + telemetry
    with client.websocket_connect(f"/ws/live?token={token}") as ws:
        # First message is the immediate full state snapshot on connect
        init_text = ws.receive_text()
        init_msg = json.loads(init_text)
        assert "speed_kph" in init_msg or "live" in init_msg or init_msg.get("type") in ("telemetry", "initial_state")

        broadcaster.broadcast_radio("driver", "Box this lap?")
        msg_text = ws.receive_text()
        msg = json.loads(msg_text)
        assert msg["type"] == "radio"
        assert msg["sender"] == "driver"
        assert msg["text"] == "Box this lap?"

    # 7. Track map API endpoints without token -> 403 Forbidden
    resp = client.get("/api/track_map")
    assert resp.status_code == 403
    resp = client.post("/api/track_map/rebuild")
    assert resp.status_code == 403

    # 8. Track map API endpoints with valid token -> 200 OK
    resp = client.get(f"/api/track_map?token={token}")
    assert resp.status_code == 200
    map_data = resp.json()
    assert map_data["has_outline"] is False

    resp = client.post(f"/api/track_map/rebuild?token={token}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rebuilding"

    # 9. Session and lap API endpoints without token -> 403 Forbidden
    assert client.get("/api/sessions").status_code == 403
    assert client.get("/api/sessions/1/laps").status_code == 403
    assert client.get("/api/laps/compare?lap1=1&lap2=2").status_code == 403
    assert client.get("/api/sessions/1/stints").status_code == 403

    # 10. Session and lap API endpoints with valid token -> 200 OK
    resp = client.get(f"/api/sessions?token={token}")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_session_adaptation_and_tyre_deg():
    """Verify session fields in snapshot, real-time tyre deg fallback, and timing tower best laps."""
    from f1telemetry.state import RaceState
    from f1telemetry.packets import Session, CarDamage, CarStatus, LapData
    import math

    state = RaceState(ptt_action=1)
    state.player = 0
    state.last_packet_time = 100.0

    # 1. Practice Session: Verify session type and real-time tyre wear fallback
    state.session = Session(
        weather=0, track_temp=35, air_temp=25, total_laps=0, track_length_m=5000,
        session_type=1, track_id=17, time_left_s=2400, pit_speed_limit=80, paused=False,
        safety_car=0, forecast=[], pit_ideal_lap=0, pit_latest_lap=0, pit_rejoin_pos=0
    )
    # Give player status (tyres 2 laps old, mediums) and damage (5.4% wear on front left)
    state.status = [CarStatus(
        fuel_mix=1, brake_bias=56, fuel_in_tank=20.0, fuel_capacity=110.0, fuel_laps_delta=0.0,
        drs_allowed=True, actual_compound=17, visual_compound=17, tyre_age_laps=2,
        fia_flag=0, ers_store_j=2000000.0, ers_mode=1
    )]
    state.damage = [CarDamage(
        tyre_wear=(4.0, 4.2, 5.4, 4.8), tyre_damage=(0, 0, 0, 0), brake_damage=(0, 0, 0, 0),
        front_left_wing=0, front_right_wing=0, rear_wing=0, floor=0, diffuser=0, sidepod=0,
        gearbox=0, engine=0
    )]
    state.lap = [LapData(
        last_lap_ms=0, current_lap_ms=45000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
        lap_distance=2500.0, position=3, lap_num=1, pit_status=0, num_pit_stops=0, sector=1,
        lap_invalid=False, penalties_s=0, result_status=2
    )]

    snap = state.snapshot()
    assert snap["is_practice"] is True
    assert snap["is_quali"] is False
    assert snap["is_race"] is False
    assert snap["session_type_name"] == "Practice 1"
    assert snap["session_time_left_s"] == 2400
    # Wear per lap must be calculated in real-time even with 0 completed laps in self.laps!
    assert snap["tyres"]["wear_pct_per_lap"] is not None
    # 5.4% / (2 + 2500/5000 = 2.5 laps) = 2.16%
    assert 2.0 <= snap["tyres"]["wear_pct_per_lap"] <= 2.3

    # 2. Qualifying Session: Check cutoff and best flying laps in timing tower
    state.session.session_type = 5  # Q1
    state.best_laps_all_cars = {0: 91500, 1: 90800}
    snap_q = state.snapshot()
    assert snap_q["is_quali"] is True
    assert snap_q["session_type_name"] == "Qualifying 1"
    assert snap_q["best_lap_s"] == 91.5
    assert snap_q["session_best_s"] == 90.8
    assert snap_q["gap_to_pole"] == 0.7

    tower = state.timing_tower()
    assert len(tower) == 1
    assert tower[0]["best_lap_s"] == 91.5
    assert tower[0]["current_lap_s"] == 45.0
    assert tower[0]["is_in_pit"] is False
