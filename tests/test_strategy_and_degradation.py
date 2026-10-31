"""Unit tests for Tyre Degradation Model, Strategy Predictor, Battle Pace, and Retirements."""
import pytest
from tyres import (
    CompoundDegradation,
    TyreDegradationModel,
    estimate_fuel_coefficient,
    fit_stint_degradation,
    fuel_corrected_lap_time,
    is_representative_lap,
)
from strategy import StrategyPlan, StrategyPredictor, UndercutOpportunity
from f1telemetry.state import RaceState, LapRecord
from f1telemetry.deterministic import route, Callouts
import f1telemetry.packets as packets


def test_representative_lap_filtering():
    """Validates filtering of non-representative laps (traffic, invalid, SC, in/out, weather mismatch)."""
    # Clean representative dry lap
    assert is_representative_lap(90.5, invalid=False, is_in_out_lap=False, sc_vsc=False, traffic_time_s=1.0, weather=0, compound_name="medium")

    # Invalid lap
    assert not is_representative_lap(90.5, invalid=True)

    # In/out lap
    assert not is_representative_lap(90.5, is_in_out_lap=True)

    # Safety car lap
    assert not is_representative_lap(90.5, sc_vsc=True)

    # Traffic > 3s
    assert not is_representative_lap(90.5, traffic_time_s=4.5)

    # Weather mismatch (rain on soft slicks)
    assert not is_representative_lap(90.5, weather=3, compound_name="soft")

    # Weather match (rain on intermediate)
    assert is_representative_lap(102.0, weather=3, compound_name="intermediate")


def test_fuel_correction_and_estimation():
    """Fuel correction removes fuel penalty, regression recovers coefficient."""
    # 30 kg fuel burned at 0.03 s/kg = 0.9s fuel effect
    t_raw = 92.9
    t_corr = fuel_corrected_lap_time(t_raw, fuel_kg=30.0, reference_fuel_kg=0.0, fuel_coef=0.03)
    assert pytest.approx(t_corr, abs=0.01) == 92.0

    # Test coefficient estimation from synthetic laps
    synthetic_laps = [
        (90.0 + (0.032 * fuel), fuel)
        for fuel in [50.0, 45.0, 40.0, 35.0, 30.0, 25.0, 20.0, 15.0]
    ]
    coef = estimate_fuel_coefficient(synthetic_laps, default_coef=0.03)
    assert 0.025 <= coef <= 0.040


def test_stint_degradation_fit_and_cliff():
    """fit_stint_degradation recovers linear deg rate and detects accelerating cliff."""
    ages = list(range(1, 16))
    # Base 90.0s, +0.12 s/lap linear deg until lap 10, then sharp cliff jump
    times = []
    for a in ages:
        t = 90.0 + 0.12 * a
        if a > 10:
            t += 0.40 * (a - 10) ** 1.5
        times.append(t)
    wears = [2.2 * a for a in ages]

    fit = fit_stint_degradation(ages, times, wears)
    assert fit["deg_s_per_lap"] > 0.10
    assert fit["cliff_lap"] is not None
    assert 8 <= fit["cliff_lap"] <= 12
    assert fit["wear_per_lap"] == pytest.approx(2.2, abs=0.3)


def test_tyre_degradation_model_classification():
    """TyreDegradationModel classifies compounds into HIGH, MEDIUM, LOW deg with confidence."""
    model = TyreDegradationModel(track_total_laps=50)

    # Feed a high degradation stint on Softs (0.22 s/lap)
    ages = list(range(1, 10))
    soft_times = [89.0 + 0.22 * a for a in ages]
    soft_deg = model.feed_stint("soft", ages, soft_times, is_player=True, is_race=True)

    assert soft_deg.deg_s_per_lap >= 0.14
    assert soft_deg.classification == "HIGH"

    # Hard compound prior / low deg
    hard_deg = model.get_compound_model("hard")
    assert hard_deg.deg_s_per_lap < 0.10


def test_strategy_plan_enumeration_and_monte_carlo():
    """StrategyPredictor enumerates plans respecting compound rules and ranks via Monte Carlo."""
    predictor = StrategyPredictor()

    plans = predictor.enumerate_plans(
        current_lap=10,
        total_laps=50,
        current_compound="medium",
        current_tyre_age=10,
        available_compounds=["soft", "medium", "hard"],
        base_lap_time=91.0,
        fuel_remaining_kg=40.0,
        dry_race=True,
    )

    # In dry race, 1-stop plan must switch to another compound (e.g. Hard or Soft, not Medium->Medium)
    assert len(plans) >= 2
    for p in plans:
        if "1-Stop" in p.name:
            assert len(set(p.compounds)) >= 2

    # Run Monte Carlo
    ranked = predictor.run_monte_carlo(plans, samples=100)
    assert len(ranked) >= 2
    assert ranked[0].is_recommended
    assert ranked[0].win_probability_pct > 0.0
    assert ranked[0].time_delta_to_best_s == 0.0


def test_undercut_opportunity_detection():
    """Detects undercut window when rival ahead is within striking distance (0.5 - 3.2s)."""
    predictor = StrategyPredictor()

    # Norris 1.8s ahead -> Box now for undercut
    opp = predictor.evaluate_undercut_threat(
        current_lap=16,
        gap_ahead_s=1.8,
        ahead_name="Norris",
        ahead_car_idx=1,
        gap_behind_s=6.5,
        behind_name="Piastri",
        behind_car_idx=2,
    )
    assert opp is not None
    assert opp.direction == "undercut"
    assert opp.recommended_action == "box_now"
    assert "undercut on Norris" in opp.text

    # Too far ahead (5.0s) -> No undercut opportunity
    no_opp = predictor.evaluate_undercut_threat(
        current_lap=16,
        gap_ahead_s=5.0,
        ahead_name="Norris",
        ahead_car_idx=1,
        gap_behind_s=6.5,
        behind_name="Piastri",
        behind_car_idx=2,
    )
    assert no_opp is None


def test_safety_car_and_rain_crossover():
    """Evaluates cheap SC pit stops and rain crossover warnings."""
    predictor = StrategyPredictor()

    # Under safety car on lap 18 with 12-lap-old tyres -> Cheap stop recommended
    sc_msg = predictor.evaluate_sc_opportunity(
        sc_status=1,
        current_lap=18,
        total_laps=50,
        current_tyre_age=12,
        current_compound="medium",
    )
    assert sc_msg is not None
    assert "cheap stop" in sc_msg.lower() or "box" in sc_msg.lower()

    # Rain forecast crossover
    class DummyForecast:
        def __init__(self, m, r):
            self.minutes_ahead = m
            self.rain_pct = r

    rain_msg = predictor.evaluate_rain_crossover(
        forecast=[DummyForecast(3, 75)],
        current_compound="medium",
    )
    assert rain_msg is not None
    assert "intermediates" in rain_msg.lower()


def test_battle_pace_and_retirements_query():
    """Tests deterministic battle pace voice query and retirement tracking."""
    st = RaceState()
    st.player = 0

    # Inject player and rivals in lap data
    class DummyLap:
        def __init__(self, pos, gap_ahead, last_ms, res=2):
            self.position = pos
            self.gap_to_car_ahead_ms = gap_ahead
            self.gap_to_leader_ms = gap_ahead
            self.last_lap_ms = last_ms
            self.current_lap_num = 12
            self.result_status = res
            self.num_pit_stops = 1
            self.penalties_s = 0

    # Car 0 (Player) in P2, Car 1 in P1 (Norris 1.6s ahead), Car 2 in P3 (Piastri 2.2s behind)
    st.lap = [
        DummyLap(2, 1600, 91500),
        DummyLap(1, 0, 91200),
        DummyLap(3, 2200, 91900),
    ]
    st.names = {0: "Player", 1: "Lando Norris", 2: "Oscar Piastri"}

    # Add player completed lap records
    st.laps.append(LapRecord(
        lap=10, lap_time_ms=91400, invalid=False, position=2,
        ahead_idx=1, gap_ahead_ms=1600, behind_idx=2, gap_behind_ms=2200,
        fuel_kg=35.0, tyre_wear=(15, 15, 20, 20), stint=1,
    ))
    st.laps.append(LapRecord(
        lap=11, lap_time_ms=91600, invalid=False, position=2,
        ahead_idx=1, gap_ahead_ms=1600, behind_idx=2, gap_behind_ms=2200,
        fuel_kg=33.3, tyre_wear=(18, 18, 23, 23), stint=1,
    ))

    # Test "lap times" query
    ans_battle = route("What are my lap times and who am I fighting?", st)
    assert ans_battle.kind == "answer"
    assert "average pace" in ans_battle.text.lower()
    assert "norris is 1.6 ahead" in ans_battle.text.lower()
    assert "piastri is 2.2 behind" in ans_battle.text.lower()

    # Test retirements query when all running
    ans_ret_clean = route("Any retirements or crashes?", st)
    assert ans_ret_clean.kind == "answer"
    assert "no retirements" in ans_ret_clean.text.lower()

    # Simulate car 3 retiring
    st.retirements.append({"car_idx": 3, "name": "Lewis Hamilton", "position": 18, "lap": 12, "reason": "Retired"})
    ans_ret_hit = route("Who crashed or retired?", st)
    assert ans_ret_hit.kind == "answer"
    assert "lewis hamilton" in ans_ret_hit.text.lower() or "hamilton" in ans_ret_hit.text.lower()


def test_proactive_strategy_plan_change():
    """Verify StrategyPredictor detects plan switch from 1-stop to 2-stop."""
    predictor = StrategyPredictor()
    predictor.reset_session()

    # Lap 2: Baseline 1-stop recommended
    p1 = StrategyPlan(name="Plan A (1-Stop)", stops=[(20, "hard")], compounds=["medium", "hard"], expected_time_s=4500.0)
    predictor.last_plans = [p1]
    alerts_l2 = predictor.evaluate_proactive_strategy(
        current_lap=2, total_laps=50, current_compound="medium", current_tyre_age=2,
    )
    assert len(alerts_l2) == 0  # Establishing baseline, no change alert yet

    # Lap 8: Strategy converts to Plan B (2-Stop)
    p2 = StrategyPlan(name="Plan B (2-Stop)", stops=[(14, "medium"), (32, "hard")], compounds=["medium", "medium", "hard"], expected_time_s=4480.0)
    predictor.last_plans = [p2]
    alerts_l8 = predictor.evaluate_proactive_strategy(
        current_lap=8, total_laps=50, current_compound="medium", current_tyre_age=8,
    )
    assert any(a["event"] == "strategy_plan_change" for a in alerts_l8)
    change_alert = next(a for a in alerts_l8 if a["event"] == "strategy_plan_change")
    assert change_alert["context"]["plan"] == "2-Stop"
    assert change_alert["context"]["lap"] == 14
    assert change_alert["priority"] == 1


def test_proactive_pit_window_and_box_calls():
    """Verify window opening notice on target_lap - 1 and box call on target_lap."""
    predictor = StrategyPredictor()
    predictor.reset_session()

    p = StrategyPlan(name="Plan A (1-Stop)", stops=[(15, "hard")], compounds=["medium", "hard"], expected_time_s=4500.0)
    predictor.last_plans = [p]

    # Lap 13: Normal running, no alerts
    a13 = predictor.evaluate_proactive_strategy(13, 50, "medium", 13)
    assert len(a13) == 0

    # Lap 14: 1 lap before target stop (target = 15)
    a14 = predictor.evaluate_proactive_strategy(14, 50, "medium", 14)
    assert any(a["event"] == "pit_window_approaching" for a in a14)
    win_alert = next(a for a in a14 if a["event"] == "pit_window_approaching")
    assert win_alert["context"]["lap"] == 15
    assert win_alert["priority"] == 1

    # Deduplication on lap 14
    a14_dup = predictor.evaluate_proactive_strategy(14, 50, "medium", 14)
    assert not any(a["event"] == "pit_window_approaching" for a in a14_dup)

    # Lap 15: Target stop lap -> Box this lap!
    a15 = predictor.evaluate_proactive_strategy(15, 50, "medium", 15)
    assert any(a["event"] == "box_this_lap" for a in a15)
    box_alert = next(a for a in a15 if a["event"] == "box_this_lap")
    assert box_alert["priority"] == 0
    assert "box" in box_alert["text"].lower()


def test_proactive_rival_behind_pitted():
    """Verify rival behind pitting triggers undercut cover callout immediately."""
    predictor = StrategyPredictor()
    predictor.reset_session()

    # Russell pits behind us
    alerts = predictor.evaluate_proactive_strategy(
        current_lap=16,
        total_laps=50,
        current_compound="medium",
        current_tyre_age=16,
        rival_behind_pitted=True,
        rival_behind_name="George Russell",
        rival_behind_gap_s=1.8,
    )
    assert any(a["event"] == "rival_pitted_cover" for a in alerts)
    alert = next(a for a in alerts if a["event"] == "rival_pitted_cover")
    assert alert["priority"] == 0
    assert "George Russell" in alert["text"]
    assert alert["context"]["name"] == "George Russell"

    # Verify deduplication
    alerts_dup = predictor.evaluate_proactive_strategy(
        current_lap=16,
        total_laps=50,
        current_compound="medium",
        current_tyre_age=16,
        rival_behind_pitted=True,
        rival_behind_name="George Russell",
        rival_behind_gap_s=1.8,
    )
    assert not any(a["event"] == "rival_pitted_cover" for a in alerts_dup)


def test_proactive_tyre_cliff_alert():
    """Verify tyre cliff warning when wear exceeds 70%."""
    predictor = StrategyPredictor()
    predictor.reset_session()

    # Normal wear 40% -> No cliff
    a_norm = predictor.evaluate_proactive_strategy(
        current_lap=18, total_laps=50, current_compound="medium", current_tyre_age=18,
        tyre_wear=(38, 40, 42, 39),
    )
    assert not any(a["event"] == "tyre_cliff" for a in a_norm)

    # Critical wear 75% -> Tyre cliff alert
    a_cliff = predictor.evaluate_proactive_strategy(
        current_lap=22, total_laps=50, current_compound="medium", current_tyre_age=22,
        tyre_wear=(65, 75, 72, 68),
    )
    assert any(a["event"] == "tyre_cliff" for a in a_cliff)
    c_alert = next(a for a in a_cliff if a["event"] == "tyre_cliff")
    assert c_alert["priority"] == 0
    assert "cliff" in c_alert["text"].lower()


def test_personality_lines_for_proactive_strategy():
    """Verify cocky personality lines load and format properly for all proactive strategy events."""
    from personality import PersonalityEngine
    from config import PersonaConfig
    pers = PersonalityEngine(PersonaConfig(name="cocky", profanity="strong"))

    # 1. strategy_plan_change
    line_plan = pers.get_reaction_text("strategy_plan_change", {"plan": "Plan B (2-Stop)", "lap": 14})
    assert line_plan is not None
    assert "Plan B (2-Stop)" in line_plan or "14" in line_plan

    # 2. pit_window_approaching
    line_win = pers.get_reaction_text("pit_window_approaching", {"lap": 18, "compound": "hards"})
    assert line_win is not None
    assert "18" in line_win and "hards" in line_win

    # 3. box_this_lap
    line_box = pers.get_reaction_text("box_this_lap", {"compound": "hards"})
    assert line_box is not None
    assert "box" in line_box.lower()

    # 4. rival_pitted_cover
    line_cov = pers.get_reaction_text("rival_pitted_cover", {"name": "Lando Norris"})
    assert line_cov is not None
    assert "Lando Norris" in line_cov

    # 5. tyre_cliff
    line_cliff = pers.get_reaction_text("tyre_cliff", {"wear": "75"})
    assert line_cliff is not None
    assert "cliff" in line_cliff.lower() or "cooked" in line_cliff.lower()

