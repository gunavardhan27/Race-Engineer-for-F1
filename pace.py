"""Pace analysis and expectation evaluation system.

Determines lap-to-lap regression, fuel burn effects, excuses, qualifying
cutoffs and pole gaps, Time Trial comparisons, expected performance vs
teammate and historical benchmarks, speed trap hints, and AI difficulty hints.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from typing import Any, Optional

from config import AppConfig
from f1telemetry import packets
from f1telemetry.deterministic import Callout
from f1telemetry.state import RUNNING, LapRecord, RaceState
from personality import PersonalityEngine
from storage import SessionStorage

log = logging.getLogger("engineer.pace")


def compute_fuel_adjusted_delta(
    rec: LapRecord,
    prev_rec: LapRecord,
    fuel_effect_s_per_kg: float = 0.03,
) -> float:
    """Computes fuel-burn adjusted lap time delta in seconds.

    Positive means the driver was slower than the previous lap, accounting for
    the car being lighter and faster from fuel burned.
    """
    raw_delta_s = (rec.lap_time_ms - prev_rec.lap_time_ms) / 1000.0
    fuel_burned_kg = 0.0
    if prev_rec.fuel_kg is not None and rec.fuel_kg is not None and prev_rec.fuel_kg > rec.fuel_kg:
        fuel_burned_kg = prev_rec.fuel_kg - rec.fuel_kg
    return raw_delta_s + (fuel_burned_kg * fuel_effect_s_per_kg)


def detect_lap_excuses(rec: LapRecord, state: RaceState) -> list[str]:
    """Detects legitimate reasons why a lap was slower, per Prompt 14 & Prompt 18 flags."""
    reasons = []

    # 1. Traffic: car within 1.5 s ahead for >30% of the lap
    if rec.traffic_pct > 0.30:
        ahead_name = state.name(rec.ahead_idx) if rec.ahead_idx is not None else None
        reasons.append(f"dirty air behind {ahead_name}" if ahead_name else "dirty air")

    # 2. Safety car or VSC
    if rec.had_safety_car:
        reasons.append("safety car")

    # 3. In/out lap or pit stop
    if rec.pit_status > 0:
        reasons.append("pit stop")
    elif rec.stint > 0 and len(state.laps) >= 2 and state.laps[-2].stint != rec.stint:
        reasons.append("pit stop")

    # 4. New damage
    if rec.new_damage:
        reasons.append("damage")

    # 5. Rain or weather change
    if rec.weather_changed:
        reasons.append("rain")

    # 6. Mistake event (spin, collision, off)
    if rec.blunder_moment:
        reasons.append(f"the moment at {rec.blunder_moment}")
    elif rec.total_warnings > 0 and len(state.laps) >= 2 and rec.total_warnings > state.laps[-2].total_warnings:
        reasons.append("the moment")

    # 7. Tyre wear above the stint's normal degradation slope
    if rec.excess_deg:
        reasons.append("tyre deg")

    return reasons


class PaceAnalyzer:
    """Orchestrates deterministic pace analysis and triggers persona reaction callouts."""

    def __init__(
        self,
        config: AppConfig,
        state: RaceState,
        personality: PersonalityEngine,
        storage: Optional[SessionStorage] = None,
    ):
        self.config = config
        self.state = state
        self.personality = personality
        self.storage = storage

        self._remark_made_on_lap: int = -1
        self._ai_hint_triggered: bool = False
        self._last_quali_safe_triggered: bool = False
        self._last_quali_danger_triggered: bool = False
        self.quiet_mode: bool = False

    def reset_session(self) -> None:
        """Resets session-level flags and rate-limiters."""
        self._remark_made_on_lap = -1
        self._ai_hint_triggered = False
        self._last_quali_safe_triggered = False
        self._last_quali_danger_triggered = False

    def trigger_pace_remark(
        self, event_name: str, context: dict[str, Any], lap_num: int
    ) -> Optional[Callout]:
        """Triggers a pace remark respecting quiet mode and the 1-per-lap rate limit."""
        if self.quiet_mode:
            return None
        if self._remark_made_on_lap == lap_num:
            return None

        c = self.personality.trigger(event_name, self.state, context)
        if c:
            self._remark_made_on_lap = lap_num
        return c

    # ---- Lap Completion Processors ----

    def on_lap(self, rec: LapRecord) -> list[Callout]:
        """Evaluates pace upon completion of a lap."""
        callouts: list[Callout] = []

        # 1. Lap-to-lap regression
        c_reg = self.evaluate_lap_regression(rec)
        if c_reg:
            callouts.append(c_reg)

        # 2. Qualifying analysis (gap to pole, drop zone)
        c_quali = self.evaluate_qualifying(rec)
        if c_quali:
            callouts.append(c_quali)

        # 3. Time Trial PB / regression
        c_tt = self.evaluate_time_trial(rec)
        if c_tt:
            callouts.append(c_tt)

        # 4. Expected performance benchmark (above/below expectation)
        c_exp = self.evaluate_expected_performance(rec)
        if c_exp:
            callouts.append(c_exp)

        return callouts

    def evaluate_lap_regression(self, rec: LapRecord) -> Optional[Callout]:
        """Compares completed valid lap against previous valid lap with fuel adjustment."""
        if rec.invalid:
            return None

        # Find previous valid lap
        prev_rec = None
        for r in reversed(list(self.state.laps)[:-1]):
            if not r.invalid and r.lap_time_ms > 0:
                prev_rec = r
                break

        if prev_rec is None:
            return None

        fuel_eff = self.config.pace.fuel_effect_s_per_kg
        adjusted_delta = compute_fuel_adjusted_delta(rec, prev_rec, fuel_effect_s_per_kg=fuel_eff)
        reasons = detect_lap_excuses(rec, self.state)

        # Update state snapshot info
        self.state.last_lap_vs_previous = {
            "delta_s": round(adjusted_delta, 3),
            "slower": adjusted_delta > 0.05,
            "reasons": reasons,
        }

        threshold = self.config.pace.slower_lap_threshold_s
        if adjusted_delta > threshold:
            if reasons:
                return self.trigger_pace_remark(
                    "slower_lap_explained",
                    {"delta": f"{adjusted_delta:.1f}", "reason": reasons[0]},
                    lap_num=rec.lap,
                )
            else:
                return self.trigger_pace_remark(
                    "slower_lap",
                    {"delta": f"{adjusted_delta:.1f}"},
                    lap_num=rec.lap,
                )

        return None

    def evaluate_qualifying(self, rec: LapRecord) -> Optional[Callout]:
        """Checks qualifying gap to pole and danger zone / safe position."""
        s = self.state.session
        if not s or not (5 <= s.session_type <= 9):
            return None

        # Gap to pole after a valid push lap
        if not rec.invalid and rec.lap_time_ms > 0:
            pole_gap = self.state.gap_to_pole_s()
            if pole_gap is not None and pole_gap > self.config.quali.large_gap_s:
                c = self.trigger_pace_remark(
                    "quali_gap_large",
                    {"gap": f"{pole_gap:.1f}"},
                    lap_num=rec.lap,
                )
                if c:
                    return c

        # Elimination cutoff check
        cutoff = self.state.elimination_cutoff(self.config.quali.eliminated_per_segment)
        if cutoff is not None and rec.position is not None:
            time_left = s.time_left_s
            # Danger zone: in drop zone late in session or on in/out lap
            if rec.position > cutoff and time_left <= 300 and not self._last_quali_danger_triggered:
                c = self.trigger_pace_remark("quali_danger_zone", {"pos": rec.position}, lap_num=rec.lap)
                if c:
                    self._last_quali_danger_triggered = True
                    return c
            # Safe: comfortably inside cutoff late in session
            elif rec.position <= cutoff and time_left <= 60 and not self._last_quali_safe_triggered:
                c = self.trigger_pace_remark("quali_safe", {"pos": rec.position}, lap_num=rec.lap)
                if c:
                    self._last_quali_safe_triggered = True
                    return c

        return None

    def evaluate_time_trial(self, rec: LapRecord) -> Optional[Callout]:
        """Compares completed lap against personal best in Time Trial."""
        tt = self.state.time_trial
        if not tt or not tt.personal_best or tt.personal_best.lap_time_ms <= 0:
            return None

        pb_ms = tt.personal_best.lap_time_ms
        if rec.lap_time_ms < pb_ms:
            gap_s = (pb_ms - rec.lap_time_ms) / 1000.0
            return self.trigger_pace_remark("tt_personal_best", {"gap": f"{gap_s:.1f}"}, lap_num=rec.lap)
        elif rec.lap_time_ms > pb_ms:
            delta_s = (rec.lap_time_ms - pb_ms) / 1000.0
            s1_loss = rec.sector1_ms - tt.personal_best.sector1_ms
            s2_loss = rec.sector2_ms - tt.personal_best.sector2_ms
            s3_loss = rec.sector3_ms - tt.personal_best.sector3_ms
            losses = [(s1_loss, 1), (s2_loss, 2), (s3_loss, 3)]
            worst_sec = max(losses, key=lambda x: x[0])[1]
            return self.trigger_pace_remark(
                "tt_slower",
                {"delta": f"{delta_s:.1f}", "sector": worst_sec},
                lap_num=rec.lap,
            )

        return None

    def evaluate_expected_performance(self, rec: LapRecord) -> Optional[Callout]:
        """Evaluates driver performance against teammate or historical database benchmark."""
        t_idx = self.state.teammate_idx()
        expected_pos = None
        basis = "none"
        confidence = 0.0
        t_name = self.state.teammate_name() or "teammate"

        # Primary benchmark: teammate in same car
        if t_idx is not None and self.state.lap and t_idx < len(self.state.lap):
            t_lap = self.state.lap[t_idx]
            if t_lap.result_status in RUNNING and t_lap.position > 0:
                expected_pos = t_lap.position
                basis = "teammate"
                confidence = 0.9

        # Fallback benchmark: historical database (requires >= 3 past sessions)
        if expected_pos is None and self.storage and self.state.session:
            s = self.state.session
            past = self.storage.get_historical_team_sessions(
                track_id=s.track_id,
                formula=s.formula,
                team_id=self.state.my_team_id,
                ai_difficulty=s.ai_difficulty,
            )
            if len(past) >= 3:
                positions = [p["position"] for p in past if p.get("position")]
                if len(positions) >= 3:
                    expected_pos = int(round(statistics.median(positions)))
                    basis = "history"
                    confidence = 0.7

        self.state.expected_performance = {
            "position": expected_pos,
            "basis": basis,
            "confidence": confidence,
        }

        # Do NOT judge the driver if benchmark data is insufficient (< 3 past sessions or no teammate)
        if expected_pos is None or basis == "none":
            return None

        margin = self.config.pace.expectation_margin
        delta_pos = rec.position - expected_pos

        if delta_pos >= margin:
            return self.trigger_pace_remark(
                "below_expectation",
                {"pos": rec.position, "expected": expected_pos, "teammate": t_name},
                lap_num=rec.lap,
            )
        elif delta_pos <= -margin:
            return self.trigger_pace_remark(
                "above_expectation",
                {"pos": rec.position, "expected": expected_pos},
                lap_num=rec.lap,
            )

        return None

    # ---- Final Classification Processors ----

    def on_final_classification(
        self, cars: list[packets.FinalClassificationData]
    ) -> list[Callout]:
        """Evaluates end-of-session teammate comparison and AI difficulty hints."""
        callouts: list[Callout] = []
        p = self.state.player
        t_idx = self.state.teammate_idx()

        if t_idx is not None and cars and p < len(cars) and t_idx < len(cars):
            my_fc = cars[p]
            t_fc = cars[t_idx]
            t_name = self.state.teammate_name() or "teammate"

            if my_fc.position > 0 and t_fc.position > 0:
                if my_fc.total_race_time > 0 and t_fc.total_race_time > 0:
                    gap = abs(my_fc.total_race_time - t_fc.total_race_time)
                elif my_fc.best_lap_time_ms > 0 and t_fc.best_lap_time_ms > 0:
                    gap = abs(my_fc.best_lap_time_ms - t_fc.best_lap_time_ms) / 1000.0
                else:
                    gap = 0.0

                gap_str = f"{gap:.1f}"

                # Big moments (bypass normal cooldowns)
                if my_fc.position < t_fc.position:
                    c = self.personality.trigger(
                        "beat_teammate", self.state, {"teammate": t_name, "gap": gap_str}
                    )
                    if c:
                        callouts.append(c)
                elif my_fc.position > t_fc.position:
                    c = self.personality.trigger(
                        "lost_to_teammate", self.state, {"teammate": t_name, "gap": gap_str}
                    )
                    if c:
                        callouts.append(c)

        # AI difficulty hint evaluation
        c_ai = self.evaluate_ai_difficulty()
        if c_ai:
            callouts.append(c_ai)

        return callouts

    def evaluate_ai_difficulty(self) -> Optional[Callout]:
        """Evaluates whether to suggest adjusting AI difficulty."""
        if self._ai_hint_triggered:
            return None

        t_idx = self.state.teammate_idx()
        s = self.state.session
        if t_idx is None or not s or not self.storage:
            return None

        my_best = self.state.get_car_best_lap_s(self.state.player)
        t_best = self.state.get_car_best_lap_s(t_idx)
        if my_best and t_best:
            # Positive delta = player faster than teammate
            delta_s = t_best - my_best
            self.storage.record_session_teammate_delta(
                self.state.session_uid or "current",
                s.ai_difficulty,
                self.state.my_team_id,
                delta_s,
            )

        history = self.storage.get_ai_difficulty_history(s.ai_difficulty, self.state.my_team_id)
        if len(history) >= 2:
            deltas = [h["delta_s"] for h in history[:3]]
            t_name = self.state.teammate_name() or "teammate"

            if all(d > 1.0 for d in deltas):
                c = self.personality.trigger("ai_too_easy", self.state, {"teammate": t_name})
                if c:
                    self._ai_hint_triggered = True
                    return c
            elif all(d < -1.0 for d in deltas):
                c = self.personality.trigger("ai_too_hard", self.state, {"teammate": t_name})
                if c:
                    self._ai_hint_triggered = True
                    return c

        return None
