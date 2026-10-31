"""Tests for Pace Analysis, Qualifying evaluation, Time Trial, and Expected Performance."""
import time
from unittest.mock import MagicMock

import pytest

from config import AppConfig, PaceConfig, QualiConfig
from f1telemetry import packets
from f1telemetry.deterministic import route
from f1telemetry.state import LapRecord, RaceState
from pace import PaceAnalyzer, compute_fuel_adjusted_delta, detect_lap_excuses
from personality import PersonalityEngine


@pytest.fixture
def pace_setup(tmp_path):
    config = AppConfig(
        pace=PaceConfig(slower_lap_threshold_s=0.5, fuel_effect_s_per_kg=0.03, expectation_margin=3),
        quali=QualiConfig(large_gap_s=1.0),
    )
    state = RaceState()
    state.player = 0
    personality = PersonalityEngine(config=config.persona)
    analyzer = PaceAnalyzer(config=config, state=state, personality=personality)
    return config, state, personality, analyzer


# ==============================================================================
# 1. Fuel Burn & Lap-to-Lap Regression
# ==============================================================================

def test_fuel_adjusted_delta_math():
    """Fuel burned makes the car lighter; doing the same time on lighter fuel is losing pace."""
    prev_lap = LapRecord(
        lap=1, lap_time_ms=90000, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=10.0, tyre_wear=None, stint=0,
    )
    # Same raw lap time (90.0s), but burned 2.0 kg of fuel
    # Fuel gain expected = 2.0 * 0.03 = 0.06s.
    curr_lap = LapRecord(
        lap=2, lap_time_ms=90000, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=8.0, tyre_wear=None, stint=0,
    )
    delta = compute_fuel_adjusted_delta(curr_lap, prev_lap, fuel_effect_s_per_kg=0.03)
    assert abs(delta - 0.06) < 1e-4


def test_slower_lap_mocking_when_no_excuse(pace_setup):
    """When slower by > 0.5s and no excuses apply, slower_lap triggers."""
    config, state, personality, analyzer = pace_setup
    prev = LapRecord(
        lap=1, lap_time_ms=80000, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=20.0, tyre_wear=(10, 10, 10, 10), stint=0,
    )
    state.laps.append(prev)

    # Lap 2 is 0.8s slower, no traffic, no damage, no safety car
    curr = LapRecord(
        lap=2, lap_time_ms=80800, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=18.5, tyre_wear=(12, 12, 12, 12), stint=0,
    )
    state.laps.append(curr)

    c = analyzer.evaluate_lap_regression(curr)
    assert c is not None
    # Cocky persona lines for slower_lap mention plus delta or coffee
    assert any(w in c.text.lower() for w in ("plus", "slower", "coffee", "tyres are fine"))
    assert state.last_lap_vs_previous["slower"] is True
    assert state.last_lap_vs_previous["reasons"] == []


def test_slower_lap_explained_traffic(pace_setup):
    """Traffic excuse: car within 1.5s ahead for > 30% of lap."""
    config, state, personality, analyzer = pace_setup
    state.names = {1: "Max Verstappen"}
    prev = LapRecord(
        lap=1, lap_time_ms=80000, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=20.0, tyre_wear=None, stint=0,
    )
    state.laps.append(prev)

    curr = LapRecord(
        lap=2, lap_time_ms=81200, invalid=False, position=3,
        ahead_idx=1, gap_ahead_ms=800, behind_idx=None, gap_behind_ms=None,
        fuel_kg=18.5, tyre_wear=None, stint=0,
        traffic_pct=0.45,  # 45% of lap behind Verstappen
    )
    state.laps.append(curr)

    c = analyzer.evaluate_lap_regression(curr)
    assert c is not None
    # Slower lap explained mentions reason without mocking
    assert any(w in c.text.lower() for w in ("dirty air", "nothing you could do", "reset", "lost"))
    assert "Verstappen" in c.text or "dirty air" in c.text


def test_slower_lap_explained_safety_car_and_damage(pace_setup):
    """Safety car or new damage explains slower lap."""
    config, state, personality, analyzer = pace_setup
    prev = LapRecord(
        lap=1, lap_time_ms=80000, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=20.0, tyre_wear=None, stint=0,
    )
    state.laps.append(prev)

    curr = LapRecord(
        lap=2, lap_time_ms=85000, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=18.5, tyre_wear=None, stint=0,
        had_safety_car=True,
    )
    state.laps.append(curr)

    c = analyzer.evaluate_lap_regression(curr)
    assert c is not None
    assert "safety car" in c.text.lower()


def test_slower_lap_threshold_not_exceeded(pace_setup):
    """If delta is <= 0.5s, no remark is made."""
    config, state, personality, analyzer = pace_setup
    prev = LapRecord(
        lap=1, lap_time_ms=80000, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=20.0, tyre_wear=None, stint=0,
    )
    state.laps.append(prev)

    # Only 0.2s slower
    curr = LapRecord(
        lap=2, lap_time_ms=80200, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=20.0, tyre_wear=None, stint=0,
    )
    state.laps.append(curr)

    c = analyzer.evaluate_lap_regression(curr)
    assert c is None


# ==============================================================================
# 2. Qualifying Cutoffs & Large Gap to Pole
# ==============================================================================

def test_dynamic_elimination_cutoff():
    """22 cars -> P16 cutoff; 20 cars -> P15 cutoff; Q2 -> P10 cutoff."""
    state = RaceState()
    # Dummy session
    state.session = packets.Session(
        weather=0, track_temp=30, air_temp=25, total_laps=0, track_length_m=5000,
        session_type=5, track_id=0, time_left_s=600, pit_speed_limit=80,
        paused=False, safety_car=0, forecast=[], pit_ideal_lap=0, pit_latest_lap=0,
        pit_rejoin_pos=0, formula=0, ai_difficulty=90,
    )
    # Simulate 22 running cars
    state.lap = [
        packets.LapData(last_lap_ms=0, current_lap_ms=0, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                        lap_distance=0, position=i+1, lap_num=1, pit_status=0, num_pit_stops=0,
                        sector=0, lap_invalid=False, penalties_s=0, result_status=2)
        for i in range(22)
    ]
    assert state.elimination_cutoff() == 16  # 22 - 6 = 16

    # Simulate 20 running cars
    state.lap = state.lap[:20]
    assert state.elimination_cutoff() == 15  # 20 - 5 = 15

    # In Q2 (session_type = 6): cutoff is top 10
    state.session.session_type = 6
    assert state.elimination_cutoff() == 10

    # Config override
    state.session.session_type = 5
    assert state.elimination_cutoff(eliminated_per_segment=4) == 16  # 20 - 4 = 16


def test_quali_gap_large_triggered(pace_setup):
    """When gap to pole exceeds config.quali.large_gap_s (1.0s), triggers quali_gap_large."""
    config, state, personality, analyzer = pace_setup
    state.session = packets.Session(
        weather=0, track_temp=30, air_temp=25, total_laps=0, track_length_m=5000,
        session_type=5, track_id=0, time_left_s=600, pit_speed_limit=80,
        paused=False, safety_car=0, forecast=[], pit_ideal_lap=0, pit_latest_lap=0,
        pit_rejoin_pos=0, formula=0, ai_difficulty=90,
    )
    # Pole time is 80.000s
    state.best_laps_all_cars[1] = 80000
    # Player did 81.500s (1.5s off pole)
    state.best_laps_all_cars[state.player] = 81500

    rec = LapRecord(
        lap=2, lap_time_ms=81500, invalid=False, position=12,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=5.0, tyre_wear=None, stint=0,
    )
    c = analyzer.evaluate_qualifying(rec)
    assert c is not None
    assert any(w in c.text.lower() for w in ("pole", "lifetime", "sightseeing"))


def test_quali_danger_zone_and_safe(pace_setup):
    """In danger zone (P16 with P15 cutoff) late in session -> quali_danger_zone; safe -> quali_safe."""
    config, state, personality, analyzer = pace_setup
    state.session = packets.Session(
        weather=0, track_temp=30, air_temp=25, total_laps=0, track_length_m=5000,
        session_type=5, track_id=0, time_left_s=180, pit_speed_limit=80,
        paused=False, safety_car=0, forecast=[], pit_ideal_lap=0, pit_latest_lap=0,
        pit_rejoin_pos=0, formula=0, ai_difficulty=90,
    )
    state.lap = [
        packets.LapData(last_lap_ms=0, current_lap_ms=0, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                        lap_distance=0, position=i+1, lap_num=1, pit_status=0, num_pit_stops=0,
                        sector=0, lap_invalid=False, penalties_s=0, result_status=2)
        for i in range(20)
    ]
    # Cutoff is 15. Driver is P17 -> drop zone
    rec = LapRecord(
        lap=3, lap_time_ms=82000, invalid=False, position=17,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=5.0, tyre_wear=None, stint=0,
    )
    c = analyzer.evaluate_qualifying(rec)
    assert c is not None
    assert any(w in c.text.lower() for w in ("drop zone", "elimination", "push"))

    # Reset cooldowns and lap rate limit for next lap
    analyzer._remark_made_on_lap = -1
    personality.last_reaction_time = -999.0
    personality.last_fired_per_event.clear()

    # Now driver improves to P8 with 40s remaining
    state.session.time_left_s = 40
    rec_safe = LapRecord(
        lap=4, lap_time_ms=80500, invalid=False, position=8,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=3.0, tyre_wear=None, stint=0,
    )
    c_safe = analyzer.evaluate_qualifying(rec_safe)
    assert c_safe is not None
    assert any(w in c_safe.text.lower() for w in ("through", "safe"))


# ==============================================================================
# 3. Time Trial
# ==============================================================================

def test_time_trial_personal_best_and_slower(pace_setup):
    """Beating PB triggers tt_personal_best; slower lap highlights worst sector loss."""
    config, state, personality, analyzer = pace_setup
    # Set PB at 80.000s (S1: 25.0s, S2: 25.0s, S3: 30.0s)
    pb_dataset = packets.TimeTrialDataSet(
        car_idx=0, team_id=1, lap_time_ms=80000,
        sector1_ms=25000, sector2_ms=25000, sector3_ms=30000,
        traction_control=0, gearbox_assist=0, anti_lock_brakes=0,
        equal_car_performance=True, custom_setup=True, valid=True,
    )
    state.time_trial = packets.TimeTrialData(
        player_session_best=pb_dataset, personal_best=pb_dataset, rival=pb_dataset
    )

    # Lap 1: Beats PB by 0.4s (79.600s)
    rec_pb = LapRecord(
        lap=1, lap_time_ms=79600, invalid=False, position=1,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=None, tyre_wear=None, stint=0,
        sector1_ms=24800, sector2_ms=24900, sector3_ms=29900,
    )
    c_pb = analyzer.evaluate_time_trial(rec_pb)
    assert c_pb is not None
    assert any(w in c_pb.text.lower() for w in ("personal best", "new pb", "quicker"))

    # Reset cooldowns and lap remark for next lap
    analyzer._remark_made_on_lap = -1
    personality.last_reaction_time = -999.0
    personality.last_fired_per_event.clear()

    # Lap 2: Slower than PB by 0.7s, lost most time in Sector 2 (25.8s vs 25.0s)
    rec_slower = LapRecord(
        lap=2, lap_time_ms=80700, invalid=False, position=1,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=None, tyre_wear=None, stint=0,
        sector1_ms=25100, sector2_ms=25800, sector3_ms=29800,
    )
    c_slower = analyzer.evaluate_time_trial(rec_slower)
    assert c_slower is not None
    assert any(w in c_slower.text.lower() for w in ("plus", "sector 2", "sector"))


# ==============================================================================
# 4. Expected Performance & Teammate Benchmarking
# ==============================================================================

def test_expected_performance_below_and_above(pace_setup):
    """>= 3 places worse than teammate -> below_expectation; >= 3 better -> above_expectation."""
    config, state, personality, analyzer = pace_setup
    state.participants = {
        0: packets.Participant("Driver", 1, 1, False),
        1: packets.Participant("Russell", 1, 63, True),
    }
    state.names = {0: "Driver", 1: "Russell"}
    # Teammate Russell is P3
    state.lap = [
        packets.LapData(last_lap_ms=80000, current_lap_ms=0, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                        lap_distance=0, position=8, lap_num=5, pit_status=0, num_pit_stops=0,
                        sector=0, lap_invalid=False, penalties_s=0, result_status=2),
        packets.LapData(last_lap_ms=79500, current_lap_ms=0, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                        lap_distance=0, position=3, lap_num=5, pit_status=0, num_pit_stops=0,
                        sector=0, lap_invalid=False, penalties_s=0, result_status=2),
    ]

    # Player is P8 (5 places worse than P3, margin is 3) -> below_expectation
    rec_below = LapRecord(
        lap=5, lap_time_ms=80000, invalid=False, position=8,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=15.0, tyre_wear=None, stint=0,
    )
    c_below = analyzer.evaluate_expected_performance(rec_below)
    assert c_below is not None
    assert "P8" in c_below.text and "P3" in c_below.text

    # Next session/stint: Player is P1, teammate is P5 (4 places better) -> above_expectation
    analyzer._remark_made_on_lap = -1
    personality.last_reaction_time = -999.0
    personality.last_fired_per_event.clear()

    state.lap[0].position = 1
    state.lap[1].position = 5
    rec_above = LapRecord(
        lap=6, lap_time_ms=79000, invalid=False, position=1,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=13.0, tyre_wear=None, stint=0,
    )
    c_above = analyzer.evaluate_expected_performance(rec_above)
    assert c_above is not None
    assert any(w in c_above.text.lower() for w in ("witchcraft", "mega", "hero", "shitbox"))


def test_expected_performance_no_data_no_remarks(pace_setup):
    """If no teammate and < 3 historical sessions, do NOT judge driver."""
    config, state, personality, analyzer = pace_setup
    # No teammate
    state.participants = {
        0: packets.Participant("Driver", 1, 1, False),
    }
    state.names = {0: "Driver"}
    state.storage = MagicMock()
    state.storage.get_historical_team_sessions.return_value = [
        {"position": 4}, {"position": 5}  # only 2 sessions (< 3)
    ]
    state.session = packets.Session(
        weather=0, track_temp=30, air_temp=25, total_laps=0, track_length_m=5000,
        session_type=10, track_id=0, time_left_s=0, pit_speed_limit=80,
        paused=False, safety_car=0, forecast=[], pit_ideal_lap=0, pit_latest_lap=0,
        pit_rejoin_pos=0, formula=0, ai_difficulty=90,
    )
    rec = LapRecord(
        lap=5, lap_time_ms=80000, invalid=False, position=15,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=15.0, tyre_wear=None, stint=0,
    )
    c = analyzer.evaluate_expected_performance(rec)
    assert c is None
    assert state.expected_performance["basis"] == "none"


def test_final_classification_beat_and_lost_to_teammate(pace_setup):
    """Session end: beat_teammate / lost_to_teammate."""
    config, state, personality, analyzer = pace_setup
    state.participants = {
        0: packets.Participant("Driver", 1, 1, False),
        1: packets.Participant("Norris", 1, 4, True),
    }
    state.names = {0: "Driver", 1: "Norris"}

    # Case 1: Driver P2, Norris P3 -> beat_teammate
    fc_beat = [
        packets.FinalClassification(position=2, num_laps=50, grid_position=2, points=18, num_pit_stops=1, result_status=3, best_lap_time_ms=79000, total_race_time=4500.0, penalties_time=0, num_penalties=0),
        packets.FinalClassification(position=3, num_laps=50, grid_position=3, points=15, num_pit_stops=1, result_status=3, best_lap_time_ms=79400, total_race_time=4503.2, penalties_time=0, num_penalties=0),
    ]
    callouts = analyzer.on_final_classification(fc_beat)
    assert len(callouts) >= 1
    assert any("Norris" in c.text for c in callouts)
    assert any("ahead" in c.text.lower() or "sulking" in c.text.lower() or "bragging" in c.text.lower() for c in callouts)

    # Case 2: Driver P4, Norris P2 -> lost_to_teammate
    fc_lost = [
        packets.FinalClassification(position=4, num_laps=50, grid_position=2, points=12, num_pit_stops=1, result_status=3, best_lap_time_ms=79500, total_race_time=4505.0, penalties_time=0, num_penalties=0),
        packets.FinalClassification(position=2, num_laps=50, grid_position=3, points=18, num_pit_stops=1, result_status=3, best_lap_time_ms=79000, total_race_time=4500.0, penalties_time=0, num_penalties=0),
    ]
    callouts_lost = analyzer.on_final_classification(fc_lost)
    assert len(callouts_lost) >= 1
    assert any("Norris" in c.text for c in callouts_lost)
    assert any("quicker" in c.text.lower() or "down" in c.text.lower() or "data" in c.text.lower() for c in callouts_lost)


# ==============================================================================
# 5. Speed Trap & Snapshot Fields
# ==============================================================================

def test_speed_trap_ranking_and_snapshot(pace_setup):
    """Driver in top third of speed trap but losing time gets straight-line hint in snapshot."""
    config, state, personality, analyzer = pace_setup
    state.speed_traps_all_cars = {
        0: 340.5,  # Player (fastest)
        1: 335.0,
        2: 330.0,
        3: 325.0,
        4: 320.0,
        5: 315.0,
    }
    # Player is losing time on last lap
    state.last_lap_vs_previous = {"delta_s": 0.8, "slower": True, "reasons": []}
    spd, rank, total, hint = state.speed_trap_ranking()
    assert spd == 340.5
    assert rank == 1
    assert total == 6
    assert hint == "car's quick on straights, you're losing in corners"

    snap = state.snapshot()
    assert snap["speed_trap"]["rank"] == 1
    assert snap["speed_trap"]["hint"] == "car's quick on straights, you're losing in corners"


# ==============================================================================
# 6. Deterministic Voice Intents
# ==============================================================================

def test_deterministic_voice_intents(pace_setup):
    """Deterministic queries: gap to pole, am I through, teammate, car capability."""
    config, state, personality, analyzer = pace_setup
    state.participants = {
        0: packets.Participant("Driver", 1, 1, False),
        1: packets.Participant("Hamilton", 1, 44, True),
    }
    state.names = {0: "Driver", 1: "Hamilton"}
    state.session = packets.Session(
        weather=0, track_temp=30, air_temp=25, total_laps=0, track_length_m=5000,
        session_type=5, track_id=0, time_left_s=300, pit_speed_limit=80,
        paused=False, safety_car=0, forecast=[], pit_ideal_lap=0, pit_latest_lap=0,
        pit_rejoin_pos=0, formula=0, ai_difficulty=90,
    )
    # 20 cars running in Q1 so cutoff is 20 - 5 = P15
    state.lap = [
        packets.LapData(last_lap_ms=81200 if i == 0 else (80500 if i == 1 else 80000 + i*100),
                        current_lap_ms=0, gap_to_car_ahead_ms=0, gap_to_leader_ms=0,
                        lap_distance=0, position=12 if i == 0 else (4 if i == 1 else i + 1),
                        lap_num=2, pit_status=0, num_pit_stops=0,
                        sector=0, lap_invalid=False, penalties_s=0, result_status=2)
        for i in range(20)
    ]
    state.best_laps_all_cars[state.player] = 81200
    state.best_laps_all_cars[1] = 80500
    state.best_laps_all_cars[2] = 80000  # Pole time

    # Intent 1: "gap to pole"
    rep1 = route("what's my gap to pole", state)
    assert rep1.kind == "answer"
    assert "1.2" in rep1.text

    # Intent 2: "am I through" (P12 <= P15 -> Safe)
    rep2 = route("am I through", state)
    assert rep2.kind == "answer"
    assert "P12" in rep2.text
    assert "Safe" in rep2.text

    # Intent 3: "where's my teammate"
    rep3 = route("where's my teammate", state)
    assert rep3.kind == "answer"
    assert "Hamilton" in rep3.text
    assert "P4" in rep3.text

    # Intent 4: "what's the car capable of"
    rep4 = route("what's the car capable of", state)
    assert rep4.kind == "answer"
    assert "Hamilton" in rep4.text
    assert "80.50" in rep4.text


# ==============================================================================
# 7. Rate Limits & Quiet Mode
# ==============================================================================

def test_rate_limit_one_pace_remark_per_lap(pace_setup):
    """At most one pace remark per lap."""
    config, state, personality, analyzer = pace_setup
    prev = LapRecord(
        lap=1, lap_time_ms=80000, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=20.0, tyre_wear=None, stint=0,
    )
    state.laps.append(prev)
    curr = LapRecord(
        lap=2, lap_time_ms=81000, invalid=False, position=3,
        ahead_idx=None, gap_ahead_ms=None, behind_idx=None, gap_behind_ms=None,
        fuel_kg=18.5, tyre_wear=None, stint=0,
    )
    state.laps.append(curr)

    c1 = analyzer.evaluate_lap_regression(curr)
    assert c1 is not None

    # Attempt second remark on same lap 2
    c2 = analyzer.trigger_pace_remark("slower_lap", {"delta": "1.0"}, lap_num=2)
    assert c2 is None


def test_quiet_mode_suppresses_pace_remarks(pace_setup):
    """Quiet mode blocks routine pace remarks."""
    config, state, personality, analyzer = pace_setup
    analyzer.quiet_mode = True
    c = analyzer.trigger_pace_remark("slower_lap", {"delta": "1.0"}, lap_num=2)
    assert c is None


# ==============================================================================
# 8. Relative Gaps (2-3s Window), Pace Deltas & Tyre Intel
# ==============================================================================

def test_evaluate_relative_gaps_catching_car_ahead(pace_setup):
    """When catching car ahead (0.4-3.5s gap, player faster), trigger catching_car_ahead with tyre info."""
    config, state, personality, analyzer = pace_setup
    # Race session (session_type=10)
    state.session = type("Session", (), {"session_type": 10, "track_id": 1, "total_laps": 50, "safety_car": 0})()
    state.player = 0

    # Laps: player (P2), car 1 ahead (P1)
    state.lap = [
        type("Lap", (), {"position": 2, "gap_to_car_ahead_ms": 2200, "result_status": 2, "last_lap_ms": 89000})(),
        type("Lap", (), {"position": 1, "gap_to_car_ahead_ms": 0, "result_status": 2, "last_lap_ms": 89500})(),
    ]
    state.names = {0: "Player", 1: "Lando Norris"}

    # Car status: compounds (16=softs), tyre ages (player=7 laps, Norris=12 laps)
    state.status = [
        type("Status", (), {"visual_compound": 16, "tyre_age_laps": 7})(),
        type("Status", (), {"visual_compound": 16, "tyre_age_laps": 12})(),
    ]

    rec = LapRecord(
        lap=5, lap_time_ms=89000, invalid=False, position=2,
        ahead_idx=1, gap_ahead_ms=2200, behind_idx=None, gap_behind_ms=None,
        fuel_kg=15.0, tyre_wear=None, stint=0,
    )
    state.laps.append(rec)

    callout = analyzer.evaluate_relative_gaps_and_tyres(rec)
    assert callout is not None
    assert "Norris" in callout.text or "hunt" in callout.text or "catch" in callout.text or "2.2" in callout.text
    # Verify tyre intel was included
    assert "softs" in callout.text or "older" in callout.text or "laps" in callout.text


def test_evaluate_relative_gaps_car_ahead_pulling_away(pace_setup):
    """When car ahead is within 2-3s but pulling away (delta <= -0.15s), urge player to fasten pace."""
    config, state, personality, analyzer = pace_setup
    state.session = type("Session", (), {"session_type": 10, "track_id": 1, "total_laps": 50, "safety_car": 0})()
    state.player = 0

    state.lap = [
        type("Lap", (), {"position": 2, "gap_to_car_ahead_ms": 2800, "result_status": 2, "last_lap_ms": 90500})(),
        type("Lap", (), {"position": 1, "gap_to_car_ahead_ms": 0, "result_status": 2, "last_lap_ms": 89800})(),
    ]
    state.names = {0: "Player", 1: "Max Verstappen"}
    state.status = [
        type("Status", (), {"visual_compound": 17, "tyre_age_laps": 10})(),
        type("Status", (), {"visual_compound": 16, "tyre_age_laps": 5})(),
    ]

    rec = LapRecord(
        lap=6, lap_time_ms=90500, invalid=False, position=2,
        ahead_idx=1, gap_ahead_ms=2800, behind_idx=None, gap_behind_ms=None,
        fuel_kg=14.0, tyre_wear=None, stint=0,
    )
    state.laps.append(rec)

    callout = analyzer.evaluate_relative_gaps_and_tyres(rec)
    assert callout is not None
    assert "Verstappen" in callout.text or "pulling away" in callout.text or "pace" in callout.text or "Speed up" in callout.text


def test_evaluate_relative_gaps_dropping_car_behind(pace_setup):
    """When car behind is within 2-3s and player is lapping faster, praise driver pace."""
    config, state, personality, analyzer = pace_setup
    state.session = type("Session", (), {"session_type": 10, "track_id": 1, "total_laps": 50, "safety_car": 0})()
    state.player = 0

    # Player is P3, car 2 is P4 (gap behind is 2.4s)
    state.lap = [
        type("Lap", (), {"position": 3, "gap_to_car_ahead_ms": 5000, "result_status": 2, "last_lap_ms": 89000})(),
        type("Lap", (), {"position": 2, "gap_to_car_ahead_ms": 1000, "result_status": 2, "last_lap_ms": 89000})(),
        type("Lap", (), {"position": 4, "gap_to_car_ahead_ms": 2400, "result_status": 2, "last_lap_ms": 89600})(),
    ]
    state.names = {0: "Player", 2: "Charles Leclerc"}
    state.status = [
        type("Status", (), {"visual_compound": 17, "tyre_age_laps": 8})(),
        type("Status", (), {"visual_compound": 17, "tyre_age_laps": 8})(),
        type("Status", (), {"visual_compound": 18, "tyre_age_laps": 15})(),
    ]

    rec = LapRecord(
        lap=8, lap_time_ms=89000, invalid=False, position=3,
        ahead_idx=1, gap_ahead_ms=5000, behind_idx=2, gap_behind_ms=2400,
        fuel_kg=12.0, tyre_wear=None, stint=0,
    )
    state.laps.append(rec)

    callout = analyzer.evaluate_relative_gaps_and_tyres(rec)
    assert callout is not None
    assert "pulling away" in callout.text or "pace" in callout.text or "slower" in callout.text or "Leclerc" in callout.text


def test_evaluate_relative_gaps_car_behind_closing(pace_setup):
    """When car behind is within 2-3s and lapping faster, warn driver to fasten pace / defend."""
    config, state, personality, analyzer = pace_setup
    state.session = type("Session", (), {"session_type": 10, "track_id": 1, "total_laps": 50, "safety_car": 0})()
    state.player = 0

    state.lap = [
        type("Lap", (), {"position": 2, "gap_to_car_ahead_ms": 6000, "result_status": 2, "last_lap_ms": 90200})(),
        type("Lap", (), {"position": 1, "gap_to_car_ahead_ms": 0, "result_status": 2, "last_lap_ms": 89000})(),
        type("Lap", (), {"position": 3, "gap_to_car_ahead_ms": 1900, "result_status": 2, "last_lap_ms": 89400})(),
    ]
    state.names = {0: "Player", 2: "George Russell"}
    state.status = [
        type("Status", (), {"visual_compound": 17, "tyre_age_laps": 14})(),
        type("Status", (), {"visual_compound": 16, "tyre_age_laps": 10})(),
        type("Status", (), {"visual_compound": 16, "tyre_age_laps": 4})(),
    ]

    rec = LapRecord(
        lap=9, lap_time_ms=90200, invalid=False, position=2,
        ahead_idx=1, gap_ahead_ms=6000, behind_idx=2, gap_behind_ms=1900,
        fuel_kg=11.0, tyre_wear=None, stint=0,
    )
    state.laps.append(rec)

    callout = analyzer.evaluate_relative_gaps_and_tyres(rec)
    assert callout is not None
    assert "defend" in callout.text or "Russell" in callout.text or "closing" in callout.text or "Speed up" in callout.text or "pace" in callout.text


def test_tyre_compound_and_age_in_state_snapshot():
    """State snapshot exposes tyre compound and tyre age for rivals ahead and behind for LLM tactical queries."""
    state = RaceState()
    state.player = 0
    state.session = packets.Session(
        weather=0, track_temp=30, air_temp=25, total_laps=50, track_length_m=5000,
        session_type=10, track_id=1, time_left_s=1000, pit_speed_limit=60, paused=False,
        safety_car=0, forecast=[], pit_ideal_lap=15, pit_latest_lap=20, pit_rejoin_pos=10
    )
    state.lap = [
        type("Lap", (), {"position": 2, "lap_num": 10, "gap_to_car_ahead_ms": 2500, "result_status": 2, "last_lap_ms": 80000, "num_pit_stops": 0})(),
        type("Lap", (), {"position": 1, "lap_num": 10, "gap_to_car_ahead_ms": 0, "result_status": 2, "last_lap_ms": 80000, "num_pit_stops": 0})(),
        type("Lap", (), {"position": 3, "lap_num": 10, "gap_to_car_ahead_ms": 1800, "result_status": 2, "last_lap_ms": 80000, "num_pit_stops": 0})(),
    ]
    state.status = [
        type("Status", (), {"visual_compound": 17, "tyre_age_laps": 8, "fuel_in_tank": 10.0, "fuel_laps_delta": 0.5, "ers_store_j": 1e6})(),
        type("Status", (), {"visual_compound": 16, "tyre_age_laps": 12, "fuel_in_tank": 10.0, "fuel_laps_delta": 0.5, "ers_store_j": 1e6})(),
        type("Status", (), {"visual_compound": 18, "tyre_age_laps": 4, "fuel_in_tank": 10.0, "fuel_laps_delta": 0.5, "ers_store_j": 1e6})(),
    ]
    state.names = {0: "Player", 1: "Norris", 2: "Russell"}

    snap = state.snapshot()
    assert snap["car_ahead"]["name"] == "Norris"
    assert snap["car_ahead"]["tyre_compound"] == "softs"
    assert snap["car_ahead"]["tyre_age_laps"] == 12

    assert snap["car_behind"]["name"] == "Russell"
    assert snap["car_behind"]["tyre_compound"] == "hards"
    assert snap["car_behind"]["tyre_age_laps"] == 4
