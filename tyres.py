"""Tyre degradation modeling, fuel correction, and stint curve fitting.

Prompt 24 Part B implementation:
- Representative lap filtering
- Fuel correction with robust regression
- Stint degradation fitting (pace s/lap, wear %/lap, cliff detection)
- Bayesian multi-source combining (my race > practice long run > history > AI)
- HIGH / MEDIUM / LOW classification with confidence metrics
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

log = logging.getLogger("engineer.tyres")


@dataclass
class CompoundDegradation:
    compound: str                         # "soft", "medium", "hard", "intermediate", "wet"
    deg_s_per_lap: float = 0.10           # pace loss in seconds per lap
    deg_std_err: float = 0.03             # standard error of the estimate
    wear_pct_per_lap: float = 2.5         # worst-corner wear % per lap
    cliff_lap: Optional[int] = None       # lap number where pace cliff occurs, if detected
    cliff_wear_pct: Optional[float] = None # wear % at cliff
    classification: str = "MEDIUM"        # "LOW", "MEDIUM", "HIGH"
    confidence: str = "low"               # "low", "medium", "high"
    sample_laps: int = 0                  # count of representative laps used
    projected_life_laps: int = 25         # estimated usable laps before cliff or 70% wear
    source_weight: float = 1.0


def is_representative_lap(
    lap_time_s: float,
    invalid: bool = False,
    is_in_out_lap: bool = False,
    sc_vsc: bool = False,
    traffic_time_s: float = 0.0,
    has_damage_change: bool = False,
    weather: int = 0,
    compound_name: str = "medium",
) -> bool:
    """Filters representative laps: clean, valid, no SC/VSC, not in/out, minimal traffic, no damage."""
    if lap_time_s <= 30.0 or lap_time_s >= 240.0:
        return False
    if invalid or is_in_out_lap or sc_vsc or has_damage_change:
        return False
    if traffic_time_s > 3.0:
        return False

    # Weather consistency check: wet/intermediate in wet, slicks in dry
    is_wet_weather = weather in (3, 4, 5)  # light rain, heavy rain, stormy
    is_wet_tyre = compound_name.lower() in ("intermediate", "inter", "wet")
    if is_wet_weather != is_wet_tyre:
        return False

    return True


def fuel_corrected_lap_time(
    lap_time_s: float,
    fuel_kg: float,
    reference_fuel_kg: float = 0.0,
    fuel_coef: float = 0.03,
) -> float:
    """Corrects lap time for fuel burned: lap_time - fuel_coef * (fuel_kg - reference_fuel)."""
    return lap_time_s - fuel_coef * (fuel_kg - reference_fuel_kg)


def estimate_fuel_coefficient(
    laps_with_fuel: list[tuple[float, float]],
    default_coef: float = 0.03,
) -> float:
    """Estimates fuel coefficient (s/kg) via linear regression across laps with fuel variation.
    
    Clamped to reasonable bounds [0.015, 0.060] s/kg (typical F1 car is ~0.030 s/kg).
    """
    if len(laps_with_fuel) < 6:
        return default_coef

    times = np.array([x[0] for x in laps_with_fuel], dtype=float)
    fuels = np.array([x[1] for x in laps_with_fuel], dtype=float)

    fuel_range = np.max(fuels) - np.min(fuels)
    if fuel_range < 5.0:  # Need meaningful fuel variation
        return default_coef

    # Simple least squares slope: lap_time vs fuel
    cov = np.cov(fuels, times)
    if cov[0, 0] <= 0:
        return default_coef
    slope = cov[0, 1] / cov[0, 0]

    # Sane clamping
    return float(np.clip(slope, 0.015, 0.060))


def fit_stint_degradation(
    ages: list[int],
    lap_times_corrected: list[float],
    wears: Optional[list[float]] = None,
) -> dict[str, Any]:
    """Fits pace degradation and wear progression for a stint.
    
    Returns:
      - base_pace: intercept (lap time on lap 0 of stint)
      - deg_s_per_lap: slope (seconds lost per lap)
      - std_err: standard error of slope
      - r2: coefficient of determination
      - cliff_lap: lap number where pace cliff occurs, or None
      - wear_per_lap: worst corner wear % per lap, or None
    """
    if len(ages) < 3 or len(ages) != len(lap_times_corrected):
        return {
            "base_pace": lap_times_corrected[0] if lap_times_corrected else 90.0,
            "deg_s_per_lap": 0.10,
            "std_err": 0.05,
            "r2": 0.0,
            "cliff_lap": None,
            "wear_per_lap": 2.5,
        }

    x = np.array(ages, dtype=float)
    y = np.array(lap_times_corrected, dtype=float)

    # Initial linear fit
    slope, intercept = np.polyfit(x, y, deg=1)

    # Outlier rejection using residuals (Huber-style)
    residuals = y - (intercept + slope * x)
    median_res = np.median(residuals)
    mad = np.median(np.abs(residuals - median_res)) or 0.1
    mask = np.abs(residuals - median_res) <= 2.5 * (1.4826 * mad)

    if np.sum(mask) >= 3:
        x_clean, y_clean = x[mask], y[mask]
        slope, intercept = np.polyfit(x_clean, y_clean, deg=1)
        res_clean = y_clean - (intercept + slope * x_clean)
        ss_res = np.sum(res_clean ** 2)
        ss_tot = np.sum((y_clean - np.mean(y_clean)) ** 2) or 1e-6
        r2 = max(0.0, 1.0 - (ss_res / ss_tot))
        n = len(x_clean)
        x_var = np.sum((x_clean - np.mean(x_clean)) ** 2) or 1e-6
        std_err = math.sqrt((ss_res / max(1, n - 2)) / x_var)
    else:
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2) or 1e-6
        r2 = max(0.0, 1.0 - (ss_res / ss_tot))
        std_err = 0.04

    # Detect Cliff (accelerating degradation / change-point)
    cliff_lap = None
    if len(x) >= 6:
        # Check if quadratic model significantly improves fit
        try:
            quad_p = np.polyfit(x, y, deg=2)  # y = a*x^2 + b*x + c
            quad_res = y - np.polyval(quad_p, x)
            quad_r2 = max(0.0, 1.0 - (np.sum(quad_res ** 2) / (ss_tot or 1e-6)))
            if quad_p[0] > 0.005 and (quad_r2 - r2) >= 0.06:
                # Find lap where instantaneous derivative exceeds 2x base slope
                # d/dx = 2*a*x + b > 2 * slope
                a_quad, b_quad = quad_p[0], quad_p[1]
                crit_lap = (2.0 * max(0.05, slope) - b_quad) / (2.0 * a_quad)
                if min(x) <= crit_lap <= max(x) + 5:
                    cliff_lap = int(round(crit_lap))
        except Exception:
            pass

        # Also check running 3-lap slope jump
        if cliff_lap is None:
            for i in range(3, len(x) - 1):
                early_slope = (y[i] - y[0]) / max(1.0, x[i] - x[0])
                late_slope = (y[-1] - y[i]) / max(1.0, x[-1] - x[i])
                if late_slope - early_slope >= 0.35:
                    cliff_lap = int(x[i])
                    break

    # Wear slope
    wear_per_lap = 2.5
    if wears and len(wears) == len(ages):
        w = np.array(wears, dtype=float)
        w_slope, _ = np.polyfit(x, w, deg=1)
        wear_per_lap = max(0.2, float(w_slope))

    return {
        "base_pace": float(intercept),
        "deg_s_per_lap": max(0.0, float(slope)),
        "std_err": float(std_err),
        "r2": float(r2),
        "cliff_lap": cliff_lap,
        "wear_per_lap": float(wear_per_lap),
    }


class TyreDegradationModel:
    """Manages multi-compound degradation models per track and session.
    
    Combines estimates using Bayesian weights:
    - Player race laps: weight 1.0
    - Player practice long runs (>=5 laps): weight 0.8
    - Database historical sessions: weight 0.5
    - AI car laps this session: weight 0.3
    """

    # Baseline priors per compound when data is thin (s/lap and wear %/lap)
    DEFAULT_PRIORS: dict[str, dict[str, float]] = {
        "soft": {"deg_s_per_lap": 0.16, "wear_pct_per_lap": 4.2, "usable_laps": 18},
        "medium": {"deg_s_per_lap": 0.10, "wear_pct_per_lap": 2.7, "usable_laps": 28},
        "hard": {"deg_s_per_lap": 0.06, "wear_pct_per_lap": 1.7, "usable_laps": 40},
        "intermediate": {"deg_s_per_lap": 0.08, "wear_pct_per_lap": 2.0, "usable_laps": 32},
        "wet": {"deg_s_per_lap": 0.05, "wear_pct_per_lap": 1.5, "usable_laps": 45},
    }

    def __init__(self, track_total_laps: int = 52, fuel_coef: float = 0.03):
        self.track_total_laps = track_total_laps
        self.fuel_coef = fuel_coef
        self.models: dict[str, CompoundDegradation] = {}
        self.stints_history: list[dict[str, Any]] = []

        # Initialize defaults
        for comp, prior in self.DEFAULT_PRIORS.items():
            self.models[comp] = self._create_prior_model(comp, prior)

    def _create_prior_model(self, compound: str, prior: dict[str, float]) -> CompoundDegradation:
        usable = int(prior.get("usable_laps", 25))
        deg = prior.get("deg_s_per_lap", 0.10)
        wear = prior.get("wear_pct_per_lap", 2.5)
        cls_label = self._classify(deg, usable, self.track_total_laps)
        return CompoundDegradation(
            compound=compound,
            deg_s_per_lap=deg,
            deg_std_err=0.04,
            wear_pct_per_lap=wear,
            cliff_lap=usable,
            cliff_wear_pct=72.0,
            classification=cls_label,
            confidence="low",
            sample_laps=0,
            projected_life_laps=usable,
            source_weight=0.2,
        )

    def _classify(self, deg_s_per_lap: float, usable_laps: int, total_laps: int) -> str:
        """Classifies degradation as HIGH, MEDIUM, or LOW based on race distance coverage."""
        frac = usable_laps / max(1, total_laps)
        if deg_s_per_lap >= 0.14 or frac < 0.35:
            return "HIGH"
        elif deg_s_per_lap <= 0.07 or frac >= 0.65:
            return "LOW"
        return "MEDIUM"

    def feed_stint(
        self,
        compound: str,
        ages: list[int],
        lap_times: list[float],
        fuel_loads: Optional[list[float]] = None,
        wears: Optional[list[float]] = None,
        is_player: bool = True,
        is_race: bool = True,
    ) -> CompoundDegradation:
        """Fits a completed or in-progress stint and updates the Bayesian combined estimate."""
        comp = compound.lower()
        if not ages or len(ages) < 3:
            return self.get_compound_model(comp)

        # Apply fuel correction
        ref_fuel = fuel_loads[0] if fuel_loads else 0.0
        corrected_times = [
            fuel_corrected_lap_time(t, f, ref_fuel, self.fuel_coef) if fuel_loads
            else t
            for t, f in zip(lap_times, fuel_loads or [0.0] * len(lap_times))
        ]

        fit = fit_stint_degradation(ages, corrected_times, wears)

        # Determine source weight
        if is_player and is_race:
            weight = 1.0
        elif is_player and len(ages) >= 5:
            weight = 0.8
        elif is_player:
            weight = 0.6
        else:
            weight = 0.3  # AI car

        curr = self.get_compound_model(comp)

        # Bayesian update of slope
        prior_deg = curr.deg_s_per_lap
        prior_var = curr.deg_std_err ** 2 or 0.0016
        obs_deg = fit["deg_s_per_lap"]
        obs_var = max(0.0004, (fit["std_err"] ** 2) / max(0.1, weight))

        # Precision-weighted mean
        new_deg = (prior_deg / prior_var + obs_deg / obs_var) / (1.0 / prior_var + 1.0 / obs_var)
        new_var = 1.0 / (1.0 / prior_var + 1.0 / obs_var)
        new_std_err = math.sqrt(new_var)

        # Wear rate update
        new_wear = (curr.wear_pct_per_lap * 0.4) + (fit["wear_per_lap"] * 0.6)

        # Usable laps projected: cliff lap or 70% wear
        wear_life = int(72.0 / max(0.5, new_wear))
        cliff = fit.get("cliff_lap") or curr.cliff_lap
        usable = min(cliff if cliff else wear_life, wear_life)

        samples = curr.sample_laps + len(ages)
        conf = "high" if samples >= 12 and new_std_err <= 0.02 else ("medium" if samples >= 5 else "low")
        cls_label = self._classify(new_deg, usable, self.track_total_laps)

        updated = CompoundDegradation(
            compound=comp,
            deg_s_per_lap=round(float(new_deg), 3),
            deg_std_err=round(float(new_std_err), 3),
            wear_pct_per_lap=round(float(new_wear), 2),
            cliff_lap=cliff,
            cliff_wear_pct=72.0,
            classification=cls_label,
            confidence=conf,
            sample_laps=samples,
            projected_life_laps=usable,
            source_weight=weight,
        )

        self.models[comp] = updated
        self.stints_history.append({"compound": comp, "fit": fit, "weight": weight, "laps": len(ages)})
        return updated

    def get_compound_model(self, compound: str) -> CompoundDegradation:
        """Returns the degradation model for a compound, falling back to medium if unknown."""
        comp = compound.lower()
        if comp in self.models:
            return self.models[comp]
        for key in ("soft", "medium", "hard", "intermediate", "wet"):
            if key in comp:
                return self.models[key]
        return self.models.get("medium", self._create_prior_model("medium", self.DEFAULT_PRIORS["medium"]))

    def summary(self) -> dict[str, Any]:
        """Summary dictionary of all compound degradation models."""
        return {
            comp: {
                "classification": m.classification,
                "confidence": m.confidence,
                "deg_s_per_lap": m.deg_s_per_lap,
                "wear_pct_per_lap": m.wear_pct_per_lap,
                "cliff_lap": m.cliff_lap,
                "projected_life_laps": m.projected_life_laps,
                "sample_laps": m.sample_laps,
            }
            for comp, m in self.models.items()
        }
