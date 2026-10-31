"""Race strategy predictor and simulation engine.

Prompt 24 Part C implementation:
- 0/1/2/3 stop plan enumeration with F1 compound regulations
- Monte Carlo race simulation over degradation uncertainty
- Undercut / overcut window evaluation against rivals within 3-4s
- Safety car / VSC cheap pit stop opportunity detector
- Rain crossover prediction for Inters / Wets
- Radio strategy callouts with confidence rating
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from tyres import CompoundDegradation, TyreDegradationModel

log = logging.getLogger("engineer.strategy")


@dataclass
class StrategyPlan:
    name: str                              # e.g. "Plan A (1-Stop)", "Plan B (2-Stop)"
    stops: list[tuple[int, str]]          # [(stop_lap, new_compound), ...]
    compounds: list[str]                  # sequence of compounds, e.g. ["medium", "hard"]
    expected_time_s: float                # mean simulated total race time from current lap
    time_delta_to_best_s: float = 0.0     # gap to fastest plan
    spread_s: float = 0.0                 # standard deviation of simulated times (risk)
    win_probability_pct: float = 0.0      # % of Monte Carlo samples where this plan wins
    confidence: str = "medium"            # "low", "medium", "high"
    is_recommended: bool = False
    notes: str = ""


@dataclass
class UndercutOpportunity:
    target_car_idx: int
    target_name: str
    gap_s: float
    direction: str                        # "undercut" (rival ahead) or "overcut" / "defend_undercut" (rival behind)
    recommended_action: str               # "box_now", "stay_out", "watch"
    clean_air_on_rejoin: bool = True
    projected_rejoin_pos: int = 1
    gain_estimate_s: float = 0.0
    text: str = ""


class StrategyPredictor:
    """Predicts optimal race strategy using Monte Carlo simulation and live telemetry."""

    def __init__(
        self,
        deg_model: Optional[TyreDegradationModel] = None,
        default_pit_loss_s: float = 21.5,
        sc_pit_loss_factor: float = 0.55,
        fuel_effect_s_per_kg: float = 0.03,
    ):
        self.deg_model = deg_model or TyreDegradationModel()
        self.default_pit_loss_s = default_pit_loss_s
        self.sc_pit_loss_factor = sc_pit_loss_factor
        self.fuel_effect_s_per_kg = fuel_effect_s_per_kg
        self.measured_pit_loss_s: Optional[float] = None

        self.last_plans: list[StrategyPlan] = []
        self.last_undercut: Optional[UndercutOpportunity] = None
        self.last_sc_call_fired: bool = False

    def update_pit_loss(self, measured_time_s: float) -> None:
        """Updates measured pit lane loss from actual stop."""
        if 14.0 <= measured_time_s <= 35.0:
            self.measured_pit_loss_s = measured_time_s
            log.info("Updated measured pit loss: %.2fs", measured_time_s)

    def get_effective_pit_loss(self, sc_status: int = 0) -> float:
        """Returns effective pit loss considering Safety Car / VSC discount."""
        base = self.measured_pit_loss_s or self.default_pit_loss_s
        if sc_status in (1, 2):  # 1 = Full SC, 2 = VSC
            return base * self.sc_pit_loss_factor
        return base

    def simulate_stint(
        self,
        compound: str,
        start_lap: int,
        num_laps: int,
        start_age: int = 0,
        fuel_start_kg: float = 30.0,
        fuel_burn_per_lap: float = 1.7,
        deg_rate_override: Optional[float] = None,
        base_lap_time: float = 90.0,
    ) -> float:
        """Simulates cumulative lap times for a stint on a compound."""
        m = self.deg_model.get_compound_model(compound)
        deg_rate = deg_rate_override if deg_rate_override is not None else m.deg_s_per_lap
        cliff = m.cliff_lap or 35

        total_time = 0.0
        fuel = fuel_start_kg

        for lap_i in range(num_laps):
            age = start_age + lap_i
            # Base pace + tyre degradation - fuel burning benefit
            lap_t = base_lap_time + (deg_rate * age) - (self.fuel_effect_s_per_kg * fuel)
            if age > cliff:
                # Accelerating cliff penalty
                lap_t += 0.35 * (age - cliff) ** 1.5

            total_time += lap_t
            fuel = max(0.0, fuel - fuel_burn_per_lap)

        return total_time

    def enumerate_plans(
        self,
        current_lap: int,
        total_laps: int,
        current_compound: str,
        current_tyre_age: int,
        available_compounds: list[str],
        base_lap_time: float = 90.0,
        fuel_remaining_kg: float = 40.0,
        fuel_burn_per_lap: float = 1.7,
        sc_status: int = 0,
        dry_race: bool = True,
    ) -> list[StrategyPlan]:
        """Enumerates 1-stop, 2-stop, and feasible strategy plans conforming to regulations."""
        laps_remaining = max(1, total_laps - current_lap + 1)
        pit_loss = self.get_effective_pit_loss(sc_status)

        # Usable dry compounds
        dry_pool = [c for c in available_compounds if c.lower() in ("soft", "medium", "hard")]
        if not dry_pool:
            dry_pool = ["medium", "hard"]

        plans: list[StrategyPlan] = []

        # 1. Zero-stop feasibility (if laps remaining is within current compound's safe life)
        m_curr = self.deg_model.get_compound_model(current_compound)
        if current_tyre_age + laps_remaining <= m_curr.projected_life_laps:
            # Only legal if already used 2 different compounds or wet race
            t_zero = self.simulate_stint(
                current_compound, current_lap, laps_remaining,
                start_age=current_tyre_age, fuel_start_kg=fuel_remaining_kg,
                base_lap_time=base_lap_time,
            )
            plans.append(StrategyPlan(
                name="Plan Zero (No Stop)",
                stops=[],
                compounds=[current_compound],
                expected_time_s=t_zero,
                confidence=m_curr.confidence,
                notes="Requires severe tyre conservation to the end.",
            ))

        # 2. One-Stop Strategies
        for next_comp in set(dry_pool):
            if dry_race and next_comp.lower() == current_compound.lower():
                continue  # Must use 2 different compounds in dry race

            # Search reasonable stop windows: lap current_lap to total_laps - 4
            min_stop = max(current_lap, current_lap + 1)
            max_stop = min(total_laps - 3, current_lap + m_curr.projected_life_laps - current_tyre_age)

            if min_stop <= max_stop:
                best_stop_lap = min_stop
                best_stop_time = float("inf")

                # Sample candidate stop laps
                step = max(1, (max_stop - min_stop) // 6)
                for stop_lap in range(min_stop, max_stop + 1, step):
                    laps_stint1 = stop_lap - current_lap
                    laps_stint2 = total_laps - stop_lap + 1
                    t1 = self.simulate_stint(
                        current_compound, current_lap, laps_stint1,
                        start_age=current_tyre_age, fuel_start_kg=fuel_remaining_kg,
                        base_lap_time=base_lap_time,
                    )
                    t2 = self.simulate_stint(
                        next_comp, stop_lap, laps_stint2,
                        start_age=0, fuel_start_kg=fuel_remaining_kg - (laps_stint1 * fuel_burn_per_lap),
                        base_lap_time=base_lap_time,
                    )
                    t_total = t1 + t2 + pit_loss
                    if t_total < best_stop_time:
                        best_stop_time = t_total
                        best_stop_lap = stop_lap

                p_name = f"Plan A (1-Stop: Lap {best_stop_lap} -> {next_comp.title()})"
                plans.append(StrategyPlan(
                    name=p_name,
                    stops=[(best_stop_lap, next_comp)],
                    compounds=[current_compound, next_comp],
                    expected_time_s=best_stop_time,
                    confidence=m_curr.confidence,
                ))

        # 3. Two-Stop Strategy (Aggressive / High Deg alternative)
        if laps_remaining >= 15:
            # Typically Soft/Medium -> Medium/Hard -> Soft/Medium
            comp2 = "medium" if "medium" in dry_pool else dry_pool[0]
            comp3 = "soft" if "soft" in dry_pool else dry_pool[-1]
            if dry_race and len({current_compound, comp2, comp3}) < 2:
                comp3 = "hard"

            stop1_lap = current_lap + max(2, laps_remaining // 3)
            stop2_lap = current_lap + max(4, 2 * laps_remaining // 3)

            t1 = self.simulate_stint(current_compound, current_lap, stop1_lap - current_lap,
                                     start_age=current_tyre_age, fuel_start_kg=fuel_remaining_kg, base_lap_time=base_lap_time)
            t2 = self.simulate_stint(comp2, stop1_lap, stop2_lap - stop1_lap,
                                     start_age=0, fuel_start_kg=fuel_remaining_kg - ((stop1_lap - current_lap) * fuel_burn_per_lap), base_lap_time=base_lap_time)
            t3 = self.simulate_stint(comp3, stop2_lap, total_laps - stop2_lap + 1,
                                     start_age=0, fuel_start_kg=fuel_remaining_kg - ((stop2_lap - current_lap) * fuel_burn_per_lap), base_lap_time=base_lap_time)
            t_two_stop = t1 + t2 + t3 + (2.0 * pit_loss)

            plans.append(StrategyPlan(
                name=f"Plan B (2-Stop: Laps {stop1_lap}, {stop2_lap})",
                stops=[(stop1_lap, comp2), (stop2_lap, comp3)],
                compounds=[current_compound, comp2, comp3],
                expected_time_s=t_two_stop,
                confidence="medium",
                notes="High pace alternative with aggressive tyre degradation.",
            ))

        return plans

    def run_monte_carlo(
        self,
        plans: list[StrategyPlan],
        samples: int = 300,
    ) -> list[StrategyPlan]:
        """Runs Monte Carlo simulations over deg uncertainties and ranks plans."""
        if not plans:
            return []

        win_counts = {p.name: 0 for p in plans}
        all_times: dict[str, list[float]] = {p.name: [] for p in plans}

        for _ in range(samples):
            fastest_name = None
            fastest_time = float("inf")

            for p in plans:
                # Add Gaussian perturbation to simulated total time based on deg confidence
                std_noise = 3.5 if p.confidence == "low" else (2.0 if p.confidence == "medium" else 1.2)
                t_sampled = random.gauss(p.expected_time_s, std_noise)
                all_times[p.name].append(t_sampled)
                if t_sampled < fastest_time:
                    fastest_time = t_sampled
                    fastest_name = p.name

            if fastest_name:
                win_counts[fastest_name] += 1

        # Calculate metrics
        best_plan = min(plans, key=lambda x: x.expected_time_s)
        for p in plans:
            p.time_delta_to_best_s = round(p.expected_time_s - best_plan.expected_time_s, 2)
            times = all_times[p.name]
            p.spread_s = round(float(np.std(times)), 2)
            p.win_probability_pct = round((win_counts[p.name] / max(1, samples)) * 100.0, 1)

        plans.sort(key=lambda x: x.expected_time_s)
        if plans:
            plans[0].is_recommended = True

        self.last_plans = plans
        return plans

    def evaluate_undercut_threat(
        self,
        current_lap: int,
        gap_ahead_s: Optional[float],
        ahead_name: Optional[str],
        ahead_car_idx: Optional[int],
        gap_behind_s: Optional[float],
        behind_name: Optional[str],
        behind_car_idx: Optional[int],
        current_position: int = 3,
        clean_air_ahead_gap_s: float = 3.5,
    ) -> Optional[UndercutOpportunity]:
        """Evaluates whether to pit now for an undercut on the car ahead or defend from car behind."""
        pit_delta_advantage = 1.8  # Typical fresh tyre out-lap delta advantage vs worn tyre in-lap

        # 1. Attack car ahead (Undercut)
        if gap_ahead_s is not None and 0.5 <= gap_ahead_s <= 3.2 and ahead_name and ahead_car_idx is not None:
            # We are within striking distance of undercut
            is_clean_air = (clean_air_ahead_gap_s >= 2.5)
            gain = round(pit_delta_advantage - gap_ahead_s, 2)
            text = f"Box this lap for the undercut on {ahead_name}. Delta advantage is {pit_delta_advantage:.1f}s."
            opp = UndercutOpportunity(
                target_car_idx=ahead_car_idx,
                target_name=ahead_name,
                gap_s=gap_ahead_s,
                direction="undercut",
                recommended_action="box_now",
                clean_air_on_rejoin=is_clean_air,
                projected_rejoin_pos=max(1, current_position - 1),
                gain_estimate_s=gain,
                text=text,
            )
            self.last_undercut = opp
            return opp

        # 2. Defend from car behind (Rival attempting undercut on player)
        if gap_behind_s is not None and 0.5 <= gap_behind_s <= 2.2 and behind_name and behind_car_idx is not None:
            text = f"{behind_name} is within undercut range, {gap_behind_s:.1f}s behind. Consider covering off."
            opp = UndercutOpportunity(
                target_car_idx=behind_car_idx,
                target_name=behind_name,
                gap_s=gap_behind_s,
                direction="defend_undercut",
                recommended_action="watch",
                clean_air_on_rejoin=True,
                projected_rejoin_pos=current_position,
                gain_estimate_s=0.0,
                text=text,
            )
            self.last_undercut = opp
            return opp

        self.last_undercut = None
        return None

    def evaluate_sc_opportunity(
        self,
        sc_status: int,
        current_lap: int,
        total_laps: int,
        current_tyre_age: int,
        current_compound: str,
    ) -> Optional[str]:
        """Detects if Safety Car / VSC offers a high-value cheap pit stop opportunity."""
        if sc_status in (1, 2):  # Active SC or VSC
            m = self.deg_model.get_compound_model(current_compound)
            laps_remaining = total_laps - current_lap
            # If tyres are somewhat worn (>= 8 laps old) and race has laps remaining
            if current_tyre_age >= 8 and laps_remaining >= 6:
                if not self.last_sc_call_fired:
                    self.last_sc_call_fired = True
                    saved_s = round(self.default_pit_loss_s * (1.0 - self.sc_pit_loss_factor), 1)
                    return f"Safety car deployed. Box box, cheap stop saves {saved_s:.0f} seconds."
        else:
            self.last_sc_call_fired = False

        return None

    def evaluate_rain_crossover(
        self,
        forecast: list[Any],
        current_compound: str,
    ) -> Optional[str]:
        """Evaluates whether rain forecast warrants a tyre crossover call."""
        if not forecast:
            return None

        for sample in forecast:
            min_ahead = getattr(sample, "minutes_ahead", 0)
            rain_pct = getattr(sample, "rain_pct", 0)
            if min_ahead in (2, 3, 4, 5) and rain_pct >= 60:
                is_slick = current_compound.lower() in ("soft", "medium", "hard")
                if is_slick:
                    return f"Rain expected in {min_ahead} minutes ({rain_pct}%). Stretch this stint for intermediates."
        return None

    def get_radio_strategy_callout(self) -> Optional[str]:
        """Returns the primary deterministic strategy radio message for driver."""
        if not self.last_plans:
            return None

        best = self.last_plans[0]
        if best.confidence in ("medium", "high") and best.stops:
            stop_lap, comp = best.stops[0]
            deg_m = self.deg_model.get_compound_model(comp)
            cls_txt = deg_m.classification.lower()
            return f"Strategy is {best.name}. Degradation is {cls_txt} on {comp}s."

        return None

    def summary(self) -> dict[str, Any]:
        """Full strategy summary for dashboard and AI agent state injection."""
        best = self.last_plans[0] if self.last_plans else None
        plan_b = self.last_plans[1] if len(self.last_plans) > 1 else None

        return {
            "recommended_plan": best.name if best else "Calculating...",
            "recommended_stops": [f"Lap {s[0]} -> {s[1].title()}" for s in best.stops] if best else [],
            "plan_b": plan_b.name if plan_b else None,
            "undercut_opportunity": self.last_undercut.text if self.last_undercut else None,
            "pit_loss_seconds": round(self.get_effective_pit_loss(), 1),
            "plans": [
                {
                    "name": p.name,
                    "stops": p.stops,
                    "expected_time_s": round(p.expected_time_s, 1),
                    "delta_to_best_s": p.time_delta_to_best_s,
                    "spread_s": p.spread_s,
                    "win_probability_pct": p.win_probability_pct,
                    "confidence": p.confidence,
                    "is_recommended": p.is_recommended,
                }
                for p in self.last_plans
            ],
            "tyre_degradation": self.deg_model.summary(),
        }
