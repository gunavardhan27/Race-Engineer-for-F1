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
