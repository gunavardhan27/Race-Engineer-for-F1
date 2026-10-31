"""Tests for personality system, shuffle bags, profanity filtering,
blunder escalation, spin heuristic, lapping filter, and LLM refusal fallback.
"""
from __future__ import annotations

import asyncio
import math
import time
from unittest.mock import MagicMock

import pytest

from agent import RaceEngineerAgent, is_refusal_or_empty
from config import AppConfig, PersonaConfig
from f1telemetry import packets
from f1telemetry.deterministic import Callout, RadioQueue
from f1telemetry.packets import CarMotion, CarTelemetry, Event, Header, LapData, PacketId
from f1telemetry.state import LapRecord, RaceState
from personality import (
    BIG_MOMENTS,
    BLUNDER_EVENTS,
    PersonaLine,
    PersonalityEngine,
    load_persona_lines,
)


class FakeLLMClient:
    def __init__(self, reply: str = ""):
        self.reply = reply

    async def stream(self, system: str, user: str, max_tokens: int = 60, timeout_s: float = 6.0):
        for word in self.reply.split():
            yield word + " "

    async def aclose(self):
        pass


# ---- 1. Profanity Filtering Tests ----

def test_profanity_filtering_none():
    engine = PersonalityEngine(config=PersonaConfig(name="cocky", profanity="none"))
    # In cocky.yaml, spin and bad_start contain mild/strong lines
    raw_lines = engine.lines.get("spin", [])
    filtered = engine._filter_lines_by_profanity(raw_lines)
    assert len(filtered) > 0
    for line in filtered:
        assert line.level == "none"


def test_profanity_filtering_mild():
    engine = PersonalityEngine(config=PersonaConfig(name="cocky", profanity="mild"))
    raw_lines = engine.lines.get("spin", [])
    filtered = engine._filter_lines_by_profanity(raw_lines)
    levels = {line.level for line in filtered}
    assert "strong" not in levels
    assert "none" in levels or "mild" in levels


def test_profanity_filtering_strong():
    engine = PersonalityEngine(config=PersonaConfig(name="cocky", profanity="strong"))
    raw_lines = engine.lines.get("spin", [])
    filtered = engine._filter_lines_by_profanity(raw_lines)
    levels = {line.level for line in filtered}
    # Strong pool includes all lines
    assert "strong" in levels or len(filtered) >= len(raw_lines)


# ---- 2. Shuffle-Bag No-Repeat Tests ----

def test_shuffle_bag_no_repeat():
    engine = PersonalityEngine(config=PersonaConfig(name="cocky", profanity="strong"))
    pool = engine._filter_lines_by_profanity(engine.lines.get("overtake_made", []))
    assert len(pool) >= 3

    seen = []
    # Pop as many lines as pool size with satisfied context
    ctx = {"name": "Hamilton", "pos": 2}
    for _ in range(len(pool)):
        line = engine._get_next_line("overtake_made", ctx)
        assert line is not None
        assert line not in seen, f"Line {line!r} repeated before pool was exhausted"
        seen.append(line)

    # Next pop will recycle the bag
    next_line = engine._get_next_line("overtake_made", ctx)
    assert next_line in seen


# ---- 3. Placeholder Fallback Tests ----

def test_placeholder_fallback_picks_satisfiable_line():
    engine = PersonalityEngine(config=PersonaConfig(name="cocky", profanity="strong"))
    # In cocky.yaml, overtake_made has lines with {name} and lines without {name}
    # If name is provided:
    line_with_ctx = engine._get_next_line("overtake_made", {"name": "Leclerc"})
    assert line_with_ctx is not None

    # If context is empty, it should pick a line without missing placeholders
    engine.reload_persona("cocky")
    line_empty_ctx = engine._get_next_line("overtake_made", {})
    assert line_empty_ctx is not None
    assert "{" not in line_empty_ctx, f"Unformatted placeholder found in {line_empty_ctx!r}"


# ---- 4. Cooldowns and Big-Moment Override Tests ----

def test_cooldowns_and_big_moment_override():
    engine = PersonalityEngine(config=PersonaConfig(name="cocky", min_gap_s=20.0))
    state = RaceState()

    now = 100.0
    c1 = engine.trigger("overtake_made", state, {"name": "Verstappen"}, now=now)
    assert c1 is not None
    assert c1.ttl == 5.0

    # Within min_gap_s, normal trigger is on cooldown
    c2 = engine.trigger("overtake_made", state, {"name": "Verstappen"}, now=now + 5.0)
    assert c2 is None

    # Big moment (win, podium, retirement) ignores cooldown
    c_win = engine.trigger("win", state, {"pos": 1}, now=now + 6.0)
    assert c_win is not None
    assert c_win.priority == 1

    c_ret = engine.trigger("retirement", state, {}, now=now + 7.0)
    assert c_ret is not None
    assert c_ret.priority == 1


# ---- 5. OVTK Lapping Filter Tests ----

def test_ovtk_lapping_filter():
    engine = PersonalityEngine(config=PersonaConfig(name="cocky"))
    state = RaceState()
    state.player = 0

    # Set up player and car 1
    state.lap = [
        LapData(last_lap_ms=85000, current_lap_ms=30000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                lap_distance=1000.0, position=3, lap_num=5, pit_status=0, num_pit_stops=0, sector=1,
                lap_invalid=False, penalties_s=0, result_status=2),
        LapData(last_lap_ms=86000, current_lap_ms=31000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                lap_distance=950.0, position=4, lap_num=5, pit_status=0, num_pit_stops=0, sector=1,
                lap_invalid=False, penalties_s=0, result_status=2),
    ]
    state.names = {1: "Max Verstappen"}

    # 1. Valid on-track overtake on same lap -> triggers reaction
    ev_same_lap = Event(code="OVTK", vehicle_idx=0, overtaking_vehicle_idx=0, being_overtaken_vehicle_idx=1)
    callout = engine.on_signal(("event", ev_same_lap), state, now=10.0)
    assert callout is not None

    # 2. Lapped car (different lap number) -> ignored
    state.lap[1] = LapData(last_lap_ms=86000, current_lap_ms=31000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                           lap_distance=950.0, position=18, lap_num=4, pit_status=0, num_pit_stops=0, sector=1,
                           lap_invalid=False, penalties_s=0, result_status=2)
    engine.reload_persona("cocky")
    callout_lapping = engine.on_signal(("event", ev_same_lap), state, now=40.0)
    assert callout_lapping is None

    # 3. Pit lane car -> ignored
    state.lap[1] = LapData(last_lap_ms=86000, current_lap_ms=31000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                           lap_distance=950.0, position=4, lap_num=5, pit_status=1, num_pit_stops=1, sector=1,
                           lap_invalid=False, penalties_s=0, result_status=2)
    callout_pit = engine.on_signal(("event", ev_same_lap), state, now=80.0)
    assert callout_pit is None


# ---- 6. Escalation Counting Tests ----

def test_blunder_escalation_counting():
    engine = PersonalityEngine(config=PersonaConfig(name="cocky"))
    state = RaceState()

    def _make_lap(num):
        return [LapData(last_lap_ms=85000, current_lap_ms=30000, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                        lap_distance=1000.0, position=3, lap_num=num, pit_status=0, num_pit_stops=0, sector=1,
                        lap_invalid=False, penalties_s=0, result_status=2)]

    state.player = 0
    state.lap = _make_lap(4)
    # Blunder 1 on lap 4: normal line
    c1 = engine.trigger("spin", state, now=10.0)
    assert c1 is not None

    # Blunder 2 on lap 5
    state.lap = _make_lap(5)
    c2 = engine.trigger("collision", state, now=45.0)
    assert c2 is not None

    # Blunder 3 on lap 6 (3 blunders in <= 5 laps) -> escalation!
    state.lap = _make_lap(6)
    c3 = engine.trigger("flashback", state, now=80.0)
    assert c3 is not None
    # Check that escalation triggered with count=3
    recent = engine.get_recent_blunders(current_lap=6, window_laps=5)
    assert len(recent) == 3


# ---- 7. Spin Heuristic on Synthetic Motion Data ----

def test_spin_heuristic_manual():
    engine = PersonalityEngine(config=PersonaConfig(spin_angle_deg=60.0, spin_entry_speed_kph=50.0, spin_exit_speed_kph=30.0, spin_window_s=3.0))

    t0 = 100.0
    # High speed and high slip angle (> 60 deg) enters spin window
    assert not engine.check_spin_manual(angle_deg=75.0, speed_kph=80.0, now=t0)

    # Within 3.0s, car slows to under 30 kph -> SPIN CONFIRMED!
    assert engine.check_spin_manual(angle_deg=30.0, speed_kph=20.0, now=t0 + 1.8)

    # Subsequent check is reset
    assert not engine.check_spin_manual(angle_deg=20.0, speed_kph=50.0, now=t0 + 2.5)


def test_spin_heuristic_timeout():
    engine = PersonalityEngine(config=PersonaConfig(spin_angle_deg=60.0, spin_entry_speed_kph=50.0, spin_exit_speed_kph=30.0, spin_window_s=3.0))

    t0 = 100.0
    # Enter spin at t0
    engine.check_spin_manual(angle_deg=80.0, speed_kph=90.0, now=t0)

    # Speed slows down, but took 4.0s (> spin_window_s of 3.0s) -> False
    assert not engine.check_spin_manual(angle_deg=10.0, speed_kph=25.0, now=t0 + 4.1)


def test_spin_heuristic_state_motion():
    engine = PersonalityEngine(config=PersonaConfig(spin_angle_deg=60.0, spin_entry_speed_kph=50.0, spin_exit_speed_kph=30.0, spin_window_s=3.0))
    state = RaceState()
    state.player = 0

    # Forward vector pointing in +Z direction: fwd_x=0, fwd_z=1
    # Velocity pointing in +X direction: vx=25 m/s (~90 kph), vz=0 (Angle = 90 deg)
    state.motion = [
        CarMotion(x=0.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0,
                  fwd_x=0.0, fwd_y=0.0, fwd_z=1.0, vx=25.0, vy=0.0, vz=0.0)
    ]
    state.tel = [
        CarTelemetry(speed_kph=90, throttle=0.0, steer=0.0, brake=0.0, gear=4, rpm=9000,
                     drs=False, brake_temps=(0, 0, 0, 0), tyre_surface_temps=(0, 0, 0, 0),
                     tyre_inner_temps=(0, 0, 0, 0), tyre_pressures=(0, 0, 0, 0))
    ]

    t0 = 200.0
    assert not engine.check_spin(state, now=t0)
    assert engine._spin_entry_time == t0

    # Now speed drops to 20 kph at t0 + 1.5s
    state.tel[0] = CarTelemetry(speed_kph=20, throttle=0.0, steer=0.0, brake=0.0, gear=1, rpm=3000,
                                drs=False, brake_temps=(0, 0, 0, 0), tyre_surface_temps=(0, 0, 0, 0),
                                tyre_inner_temps=(0, 0, 0, 0), tyre_pressures=(0, 0, 0, 0))
    state.motion[0] = CarMotion(x=0.0, z=0.0, yaw=0.0, g_lat=0.0, g_lon=0.0, g_vert=0.0,
                                fwd_x=0.0, fwd_y=0.0, fwd_z=1.0, vx=5.0, vy=0.0, vz=0.0)
    assert engine.check_spin(state, now=t0 + 1.5)


# ---- 8. LLM Refusal and Apology Fallback Tests ----

def test_llm_refusal_fallback():
    async def _run():
        radio = RadioQueue()
        # Fake LLM refusing or apologizing
        fake_llm = FakeLLMClient("I apologize, but as an AI language model I cannot answer that.")
        agent = RaceEngineerAgent(llm=fake_llm, radio=radio, persona_name="cocky", profanity="mild")

        reply = await agent.answer("How's my pace?", {"lap": 8}, fallback_text="Pace is fine, mate.")
        assert reply == "Pace is fine, mate."
        assert not is_refusal_or_empty(reply)

        c = radio.pop()
        assert c is not None
        assert c.text == "Pace is fine, mate."

    asyncio.run(_run())


def test_post_race_message_llm_refusal_fallback():
    async def _run():
        radio = RadioQueue()
        fake_llm = FakeLLMClient("I'm sorry, I cannot fulfill this request.")
        agent = RaceEngineerAgent(llm=fake_llm, radio=radio, persona_name="cocky", profanity="mild")

        summary = {
            "grid": 3,
            "finish_position": 1,
            "positions_gained": 2,
            "fastest_lap": True,
            "penalties_s": 0,
            "blunder_count": 0,
            "best_lap": "1:24.32s",
        }
        post_race = await agent.generate_post_race_message(summary)
        assert post_race is not None
        assert not is_refusal_or_empty(post_race)
        assert "P1" in post_race or "won" in post_race

    asyncio.run(_run())


def test_dashboard_persona_settings_api():
    from fastapi.testclient import TestClient
    from dashboard.server import create_app, Broadcaster
    from main import Engineer

    cfg = AppConfig()
    cfg.persona.name = "cocky"
    cfg.persona.profanity = "mild"
    state = RaceState()
    engineer = Engineer(config=cfg)

    app = create_app(
        state=state,
        broadcaster=Broadcaster(),
        token="testtoken",
        config=cfg,
        engineer=engineer,
    )
    client = TestClient(app)

    # 1. GET settings without token -> 403
    r = client.get("/api/settings/persona")
    assert r.status_code == 403

    # 2. GET settings with token -> 200
    r = client.get("/api/settings/persona?token=testtoken")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "cocky"
    assert data["profanity"] == "mild"
    assert "cocky" in data["available_personas"]
    assert "professional" in data["available_personas"]

    # 3. POST live settings update
    payload = {
        "name": "professional",
        "profanity": "none",
        "reaction_frequency": "low",
        "min_gap_s": 35.0,
    }
    r = client.post("/api/settings/persona?token=testtoken", json=payload)
    assert r.status_code == 200
    res = r.json()
    assert res["status"] == "ok"
    assert res["name"] == "professional"
    assert res["profanity"] == "none"

    # Verify live update propagated to engineer config & personality
    assert engineer.config.persona.name == "professional"
    assert engineer.config.persona.profanity == "none"
    assert engineer.personality.config.name == "professional"


def test_post_session_points_rules():
    """Verify official FIA points rules:
    - Practice & Quali: 0 points
    - Race: P1=25, P2=18, P3=15, P4=12, P5=10, P6=8, P7=6, P8=4, P9=2, P10=1, P11+=0
    """
    from f1telemetry.state import RaceState, F1_RACE_POINTS
    from f1telemetry.packets import Session

    state = RaceState()

    # 1. Practice (session_type 1..4): 0 points regardless of position
    state.session = Session(
        weather=0, track_temp=30, air_temp=25, total_laps=20, track_length_m=5000,
        session_type=1, track_id=7, time_left_s=1800, pit_speed_limit=80,
        paused=False, safety_car=0, forecast=[], pit_ideal_lap=10, pit_latest_lap=12, pit_rejoin_pos=10
    )
    assert state.is_practice is True
    assert state.is_quali is False
    assert state.is_race is False
    for p in range(1, 21):
        assert state.get_points(p) == 0

    # 2. Qualifying (session_type 5..9): 0 points regardless of position
    state.session.session_type = 5
    assert state.is_practice is False
    assert state.is_quali is True
    assert state.is_race is False
    for p in range(1, 21):
        assert state.get_points(p) == 0

    # 3. Race (session_type 10): Official FIA points
    state.session.session_type = 10
    assert state.is_practice is False
    assert state.is_quali is False
    assert state.is_race is True

    expected_pts = {
        1: 25, 2: 18, 3: 15, 4: 12, 5: 10,
        6: 8, 7: 6, 8: 4, 9: 2, 10: 1,
        11: 0, 12: 0, 20: 0,
    }
    for pos, pts in expected_pts.items():
        assert state.get_points(pos) == pts


def test_post_session_agent_messages_no_mistakes():
    """Verify post-session messages do not count mistakes and get facts right on points."""
    import asyncio
    from agent import RaceEngineerAgent
    from f1telemetry.deterministic import RadioQueue

    async def _run():
        radio = RadioQueue()
        fake_llm = FakeLLMClient("I'm sorry, I cannot fulfill this request.")
        agent = RaceEngineerAgent(llm=fake_llm, radio=radio, persona_name="cocky", profanity="none")

        # Practice
        prac_summary = {
            "session_type": "practice",
            "finish_position": 4,
            "points": 0,
        }
        prac_msg = await agent.generate_post_race_message(prac_summary)
        assert "points" not in prac_msg.lower()
        assert "P4" in prac_msg
        assert "practice" in prac_msg.lower() or "data" in prac_msg.lower() or "box" in prac_msg.lower()

        # Quali - Pole
        pole_summary = {
            "session_type": "qualifying",
            "finish_position": 1,
            "points": 0,
        }
        pole_msg = await agent.generate_post_race_message(pole_summary)
        assert "points" not in pole_msg.lower()
        assert "pole" in pole_msg.lower()

        # Quali - P6
        q_summary = {
            "session_type": "qualifying",
            "finish_position": 6,
            "points": 0,
        }
        q_msg = await agent.generate_post_race_message(q_summary)
        assert "points" not in q_msg.lower()
        assert "P6" in q_msg
        assert "grid" in q_msg.lower()

        # Race - P1 (25 pts)
        r1_summary = {
            "session_type": "race",
            "finish_position": 1,
            "points": 25,
        }
        r1_msg = await agent.generate_post_race_message(r1_summary)
        assert "25" in r1_msg
        assert "P1" in r1_msg

        # Race - P2 (18 pts)
        r2_summary = {
            "session_type": "race",
            "finish_position": 2,
            "points": 18,
        }
        r2_msg = await agent.generate_post_race_message(r2_summary)
        assert "18" in r2_msg
        assert "P2" in r2_msg

        # Race - P5 (10 pts)
        r5_summary = {
            "session_type": "race",
            "finish_position": 5,
            "points": 10,
        }
        r5_msg = await agent.generate_post_race_message(r5_summary)
        assert "10 points" in r5_msg
        assert "P5" in r5_msg

        # Race - P10 (1 pt)
        r10_summary = {
            "session_type": "race",
            "finish_position": 10,
            "points": 1,
        }
        r10_msg = await agent.generate_post_race_message(r10_summary)
        assert "1 point" in r10_msg
        assert "P10" in r10_msg

        # Race - P14 (0 pts)
        r14_summary = {
            "session_type": "race",
            "finish_position": 14,
            "points": 0,
        }
        r14_msg = await agent.generate_post_race_message(r14_summary)
        assert "Outside the points" in r14_msg

    asyncio.run(_run())


def test_personality_finished_signal_practice_and_quali():
    """Verify finish signal in personality respects practice (no points) and quali (pole/grid)."""
    from personality import PersonalityEngine
    from f1telemetry.state import RaceState
    from f1telemetry.packets import Session

    pers = PersonalityEngine()
    state = RaceState()
    state.session_uid = 9911
    state.session = Session(
        weather=0, track_temp=30, air_temp=25, total_laps=20, track_length_m=5000,
        session_type=1, track_id=7, time_left_s=1800, pit_speed_limit=80,
        paused=False, safety_car=0, forecast=[], pit_ideal_lap=10, pit_latest_lap=12, pit_rejoin_pos=10
    )

    # 1. Practice finish
    info_p = {"position": 5, "gap_ahead_ms": 1000, "gap_behind_ms": 1500, "penalties_s": 0, "session_uid": 9911}
    c_p = pers.on_signal(("finished", info_p), state, now=10.0)
    assert c_p is not None
    assert "points" not in c_p.text.lower()
    assert "practice" in c_p.text.lower() or "box" in c_p.text.lower()

    # 2. Quali Pole finish
    state.session_uid = 9912
    state.session.session_type = 5  # Q1
    info_q1 = {"position": 1, "gap_ahead_ms": 0, "gap_behind_ms": 250, "penalties_s": 0, "session_uid": 9912}
    c_q1 = pers.on_signal(("finished", info_q1), state, now=20.0)
    assert c_q1 is not None
    assert "points" not in c_q1.text.lower()
    assert "pole" in c_q1.text.lower()

    # 3. Quali P4 finish
    state.session_uid = 9913
    info_q4 = {"position": 4, "gap_ahead_ms": 300, "gap_behind_ms": 400, "penalties_s": 0, "session_uid": 9913}
    c_q4 = pers.on_signal(("finished", info_q4), state, now=30.0)
    assert c_q4 is not None
    assert "points" not in c_q4.text.lower()
    assert any(w in c_q4.text.lower() for w in ("grid", "race", "sunday"))

