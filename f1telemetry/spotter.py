"""Deterministic live spotter / proximity radar using 20 Hz Motion telemetry.

Computes relative position of opponent cars in the player car's coordinate frame
using worldForwardDir and worldRightDir vectors. Provides instantaneous audio cues:
- 'Car left' / 'Car right'
- 'Three wide'
- 'Still there'
- 'Clear'
"""
from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Optional

from config import SpotterConfig
from .state import RaceState

log = logging.getLogger("engineer.spotter")

SPOTTER_PHRASES = (
    "Car left",
    "Car right",
    "Three wide",
    "Still there",
    "Clear",
)


class SpotterState(str, Enum):
    CLEAR = "clear"
    CAR_LEFT = "left"
    CAR_RIGHT = "right"
    THREE_WIDE = "three_wide"


class Spotter:
    def __init__(self, config: Optional[SpotterConfig] = None):
        self.config = config or SpotterConfig()
        self.state: SpotterState = SpotterState.CLEAR
        self.last_callout_time: float = 0.0
        self.last_occupied_time: float = 0.0
        self.clear_debounce_s: float = 0.25

    def reset(self) -> None:
        """Resets spotter tracking state."""
        self.state = SpotterState.CLEAR
        self.last_callout_time = 0.0
        self.last_occupied_time = 0.0

    def process(self, state: RaceState, now: Optional[float] = None) -> Optional[str]:
        """Evaluates car motion and returns a spotter phrase if state changed, or None."""
        if not self.config.enabled:
            return None

        if now is None:
            now = time.monotonic()

        # Speed filter: disable at pit speeds, grid, or spin recovery
        p_tel = state.my_telemetry
        speed_kph = p_tel.speed_kph if p_tel else 0.0
        if speed_kph < self.config.min_speed_kph:
            self.state = SpotterState.CLEAR
            return None

        p_idx = state.player
        if state.motion is None or p_idx >= len(state.motion) or state.lap is None or p_idx >= len(state.lap):
            return None

        p_mot = state.motion[p_idx]
        if p_mot is None:
            return None

        # Player direction unit vectors
        fwd_x, fwd_y, fwd_z = p_mot.fwd_x, p_mot.fwd_y, p_mot.fwd_z
        rt_x, rt_y, rt_z = p_mot.right_x, p_mot.right_y, p_mot.right_z

        left_occupied = False
        right_occupied = False

        for i, opp_mot in enumerate(state.motion):
            if i == p_idx or opp_mot is None:
                continue

            # Lap data filter: car must be active and not in pit lane
            if i >= len(state.lap):
                continue
            opp_lap = state.lap[i]
            if opp_lap is None:
                continue

            # Must be active running car (result_status == 2)
            if opp_lap.result_status != 2:
                continue
            # Must not be in pit lane (pit_status == 0)
            if opp_lap.pit_status != 0:
                continue

            # Crossover filter: vertical distance check (|ΔY| <= 3.0 m)
            dy = opp_mot.y - p_mot.y
            if abs(dy) > 3.0:
                continue

            dx = opp_mot.x - p_mot.x
            dz = opp_mot.z - p_mot.z

            # Project into player coordinate frame
            d_forward = dx * fwd_x + dy * fwd_y + dz * fwd_z
            d_right = dx * rt_x + dy * rt_y + dz * rt_z

            # Check longitudinal overlap (e.g. within 4.8m)
            if abs(d_forward) <= self.config.longitudinal_distance_m:
                # Check lateral proximity
                if 1.0 <= d_right <= self.config.lateral_distance_m:
                    right_occupied = True
                elif -self.config.lateral_distance_m <= d_right <= -1.0:
                    left_occupied = True

        # Target state determination
        if left_occupied and right_occupied:
            target = SpotterState.THREE_WIDE
        elif left_occupied:
            target = SpotterState.CAR_LEFT
        elif right_occupied:
            target = SpotterState.CAR_RIGHT
        else:
            target = SpotterState.CLEAR

        # State machine transitions
        if target != self.state:
            if target == SpotterState.THREE_WIDE:
                self.state = target
                self.last_callout_time = now
                self.last_occupied_time = now
                return "Three wide"

            elif target == SpotterState.CAR_LEFT:
                self.state = target
                self.last_callout_time = now
                self.last_occupied_time = now
                return "Car left"

            elif target == SpotterState.CAR_RIGHT:
                self.state = target
                self.last_callout_time = now
                self.last_occupied_time = now
                return "Car right"

            elif target == SpotterState.CLEAR:
                # Debounce clear transition to prevent chattering
                if now - self.last_occupied_time >= self.clear_debounce_s:
                    self.state = SpotterState.CLEAR
                    self.last_callout_time = now
                    return "Clear"
                return None

        else:
            # Maintained occupied state
            if self.state != SpotterState.CLEAR:
                self.last_occupied_time = now
                if now - self.last_callout_time >= self.config.still_there_interval_s:
                    self.last_callout_time = now
                    return "Still there"
            return None
