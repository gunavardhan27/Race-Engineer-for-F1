"""Overtake detection v2, track-model feature extraction, classification, and corner defend warnings.

Part B of Prompt 21:
- Order tracking using total_distance for all 24 cars within 60m
- Confirmed after 2.0s or 80m travelled with sign held
- Multi-car track model integration for braking zones from Lap 1
- Full classification: clean, brave, divebomb, divebomb_contact, overtake_mode_pass,
  around_the_outside, switchback, passed_stricken_car
- Lost passes classified: divebombed_by, fair_pass_against, forced_off_by
- Defend warnings from track model braking zones with inside-look detection
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from config import DefendConfig, OvertakeConfig
from f1telemetry.packets import NUM_CARS
from f1telemetry.spotter import Spotter, SpotterState
from f1telemetry.state import RaceState
from trackmodel import Corner, TrackModel

log = logging.getLogger("engineer.overtakes")
log_detectors = logging.getLogger("engineer.detectors")

OFF_TRACK_SURFACES = {4, 5, 6, 7, 8}  # Gravel, Mud, Sand, Grass, Water


@dataclass
class BrakingZone:
    corner_number: int          # 1, 2, ...
    name: str                   # "Turn 1" or from track knowledge
    start_dist_m: float         # Start of braking (where brake > 0.2)
    apex_dist_m: float          # Apex distance (where speed is min in zone)
    ref_braking_start_m: float  # Reference braking start
    ref_min_speed_kph: float    # Min speed at apex in reference lap
    ref_exit_speed_kph: float   # Speed at apex + 100m in reference lap
    direction: str              # "left" or "right"


class BrakingZoneDetector:
    """Extracts and maintains braking zones from reference lap traces per track."""

    def __init__(self, knowledge_base: Any | None = None):
        self.knowledge = knowledge_base
        self.zones_by_track: dict[int, list[BrakingZone]] = {}
        self.ref_lap_time_by_track: dict[int, float] = {}

    def update_from_trace(
        self,
        track_id: int,
        lap_time_s: float,
        trace: dict[str, np.ndarray],
    ) -> list[BrakingZone]:
        """Recomputes braking zones if this lap is faster than current reference or initial."""
        if track_id in self.ref_lap_time_by_track and lap_time_s >= self.ref_lap_time_by_track[track_id]:
            return self.zones_by_track.get(track_id, [])

        zones = self._extract_zones(track_id, trace)
        if zones:
            self.zones_by_track[track_id] = zones
            self.ref_lap_time_by_track[track_id] = lap_time_s
            log.info("Computed %d braking zones for track %d (ref lap %.3fs)", len(zones), track_id, lap_time_s)
        return zones

    def get_zones(self, track_id: int) -> list[BrakingZone]:
        return self.zones_by_track.get(track_id, [])

    def set_zones(self, track_id: int, zones: list[BrakingZone]):
        self.zones_by_track[track_id] = zones

    def _extract_zones(self, track_id: int, trace: dict[str, np.ndarray]) -> list[BrakingZone]:
        dist = trace.get("distance")
        speed = trace.get("speed")
        brake = trace.get("brake")
        steer = trace.get("steer")

        if dist is None or speed is None or brake is None or len(dist) < 20:
            return []

        corner_names = []
        if self.knowledge:
            corner_names = self.knowledge.get_corner_names(track_id) if hasattr(self.knowledge, "get_corner_names") else []

        zones: list[BrakingZone] = []
        in_braking = False
        start_idx = 0
        n = len(dist)
        corner_counter = 1

        for i in range(n):
            b_val = float(brake[i])
            if not in_braking and b_val > 0.2:
                in_braking = True
                start_idx = i
            elif in_braking and (b_val <= 0.05 or i == n - 1):
                in_braking = False
                end_idx = i
                lookahead_idx = min(n - 1, end_idx + int(100.0 / 5.0))
                window_speeds = speed[start_idx : lookahead_idx + 1]
                if len(window_speeds) == 0:
                    continue
                apex_rel_idx = int(np.argmin(window_speeds))
                apex_idx = start_idx + apex_rel_idx

                apex_dist = float(dist[apex_idx])
                start_dist = float(dist[start_idx])
                min_spd = float(speed[apex_idx])

                exit_idx = min(n - 1, apex_idx + int(100.0 / 5.0))
                exit_spd = float(speed[exit_idx])

                apex_steer = float(steer[apex_idx]) if steer is not None and len(steer) > apex_idx else 0.0
                direction = "right" if apex_steer >= 0 else "left"

                c_name = f"Turn {corner_counter}"
                if isinstance(corner_names, dict) and corner_counter in corner_names:
                    c_name = corner_names[corner_counter]
                elif isinstance(corner_names, list) and corner_counter - 1 < len(corner_names):
                    c_name = corner_names[corner_counter - 1]

                zones.append(BrakingZone(
                    corner_number=corner_counter,
                    name=c_name,
                    start_dist_m=start_dist,
                    apex_dist_m=apex_dist,
                    ref_braking_start_m=start_dist,
                    ref_min_speed_kph=min_spd,
                    ref_exit_speed_kph=exit_spd,
                    direction=direction,
                ))
                corner_counter += 1

        return zones


@dataclass
class OvertakeCandidate:
    car_idx: int
    direction: str             # "pass" or "loss"
    start_time: float
    initial_my_total: float
    initial_their_total: float
    is_stricken: bool = False
    corner: Corner | None = None
    braking_zone: BrakingZone | None = None
    location: str = "straight" # "straight", "braking_zone", "mid_corner", "exit"
    side: str = "inside"       # "inside" or "outside"
    braking_delta_m: float | None = None
    brake_vs_field_m: float | None = None
    start_gap_m: float | None = None
    overlap_at_turn_in: bool = False
    player_min_speed: float = 999.0
    player_exit_speed: float | None = None
    other_pre_apex_speed: float | None = None
    other_exit_speed: float | None = None
    other_warnings_start: int = 0
    other_invalid_start: bool = False
    turn_in_recorded: bool = False
    ran_deep: bool = False
    victim_reaction: bool = False
    forced_off: bool = False
    overtake_mode_active: bool = False
    slipstream: bool = False
    is_backmarker: bool = False


class OvertakeDetector:
    """Deterministic telemetry-based overtake detection and classification (Prompt 21)."""

    def __init__(
        self,
        config: OvertakeConfig | None = None,
        storage: Any | None = None,
        braking_zones: BrakingZoneDetector | None = None,
        track_model: TrackModel | None = None,
    ):
        self.config = config or OvertakeConfig()
        self.storage = storage
        self.braking_zones = braking_zones or BrakingZoneDetector()
        self.track_model = track_model

        # Sign tracking: car_idx -> last known sign of (my_total - their_total)
        self._prev_signs: dict[int, int] = {}
        self._prev_pit_status: dict[int, int] = {}
        self._pit_exit_time: dict[int, float] = {}

        # Active candidates: car_idx -> OvertakeCandidate
        self._candidates: dict[int, OvertakeCandidate] = {}

        # Collision events: deque of (time, v1, v2, severity)
        self._recent_collisions: deque[tuple] = deque(maxlen=50)

        # Recent confirmed overtakes: deque of record dicts
        self._recent_overtakes: deque[dict[str, Any]] = deque(maxlen=50)

        # Speed history for stricken car detection
        self._speed_history: dict[int, deque[tuple[float, float]]] = {}
        self._player_speed_history: deque[tuple[float, float]] = deque(maxlen=120)

        # Telemetry history for victim reaction
        self._opponent_inputs: dict[int, deque[tuple[float, float, float, float]]] = {}  # (time, brake, throttle, steer)

        # Slipstream tracking: car_idx -> last time player was behind < 1.0s on straight
        self._slipstream_duration: dict[int, float] = {}

        # Overtake mode active history: deque of (time, bool)
        self._overtake_mode_history: deque[tuple[float, bool]] = deque(maxlen=120)

    def set_track_model(self, model: TrackModel):
        self.track_model = model

    def on_collision(self, v1: int, v2: int, severity: int = 1, now: float | None = None):
        """Records COLL event for contact feature matching."""
        if now is None:
            now = time.monotonic()
        self._recent_collisions.append((now, v1, v2, severity))

    def update(self, state: RaceState, now: float | None = None) -> list[dict[str, Any]]:
        """Evaluates live telemetry and returns confirmed overtake/loss records."""
        if now is None:
            now = time.monotonic()

        signals: list[dict[str, Any]] = []
        p_idx = state.player
        if state.lap is None or p_idx >= len(state.lap):
            return signals

        me = state.lap[p_idx]
        if me is None:
            return signals

        my_total = getattr(me, "total_distance", 0.0)
        my_lap_dist = me.lap_distance
        my_lap_num = me.lap_num
        my_speed = state.my_telemetry.speed_kph if state.my_telemetry else 0.0
        self._player_speed_history.append((now, my_speed))

        # Track player's Overtake Mode state (CarTelemetry2)
        my_ot_active = False
        if hasattr(state, "my_tel2") and state.my_tel2 is not None:
            my_ot_active = bool(state.my_tel2.overtake_active)
        self._overtake_mode_history.append((now, my_ot_active))

        track_id = state.session.track_id if state.session else 0
        track_len = state.track_length
        zones = self.braking_zones.get_zones(track_id)

        # Clean old speed history
        cutoff = now - 4.0
        for c_idx in list(self._speed_history.keys()):
            q = self._speed_history[c_idx]
            while q and q[0][0] < cutoff:
                q.popleft()

        num_cars = len(state.lap)
        for i in range(num_cars):
            if i == p_idx:
                continue
            other = state.lap[i]
            if other is None:
                continue

            # Track pit status
            old_pit = self._prev_pit_status.get(i, 0)
            if old_pit != 0 and other.pit_status == 0:
                self._pit_exit_time[i] = now
            self._prev_pit_status[i] = other.pit_status

            # Approximate other car speed from Motion
            other_speed = 0.0
            if state.motion and i < len(state.motion) and state.motion[i] is not None:
                mot = state.motion[i]
                other_speed = math.sqrt(mot.vx ** 2 + mot.vy ** 2 + mot.vz ** 2) * 3.6
            if i not in self._speed_history:
                self._speed_history[i] = deque(maxlen=120)
            self._speed_history[i].append((now, other_speed))

            # Track opponent inputs for victim reaction
            if state.tel and i < len(state.tel) and state.tel[i] is not None:
                o_tel = state.tel[i]
                if i not in self._opponent_inputs:
                    self._opponent_inputs[i] = deque(maxlen=60)
                self._opponent_inputs[i].append((now, o_tel.brake, o_tel.throttle, o_tel.steer))

            # Primary signal: total_distance difference
            their_total = getattr(other, "total_distance", 0.0)
            dist_diff = my_total - their_total
            abs_gap = abs(dist_diff)

            # Slipstream tracking on straight: within 1.0s behind (e.g. 0 < their_total - my_total < 50m)
            is_on_straight = True
            if self.track_model:
                curr_corner = self.track_model.get_corner_at_dist(my_lap_dist)
                if curr_corner is not None:
                    is_on_straight = False
            if is_on_straight and -50.0 <= dist_diff < 0:
                prev_slip = self._slipstream_duration.get(i, 0.0)
                self._slipstream_duration[i] = prev_slip + 0.1
            else:
                self._slipstream_duration[i] = 0.0

            # Proximity check (default 60m per Prompt 21)
            prox_m = getattr(self.config, "proximity_m", 60.0)
            if abs_gap < prox_m:
                sign = 1 if dist_diff > 0 else -1
                prev_sign = self._prev_signs.get(i)
                self._prev_signs[i] = sign

                if prev_sign is not None and sign != prev_sign:
                    # If this car was already an active candidate, sign flipped -> side-by-side flicker!
                    if i in self._candidates:
                        log_detectors.info(
                            "Overtake candidate car %d, confirmed=False, reason=side_by_side_flicker (elapsed=%.2fs)",
                            i, now - self._candidates[i].start_time
                        )
                        del self._candidates[i]
                        continue

                    candidate_dir = "pass" if sign > 0 else "loss"
                    is_rejected = False
                    reject_reason = ""
                    is_backmarker = False
                    if candidate_dir == "pass" and my_lap_num > other.lap_num:
                        is_backmarker = True

                    # 1. Other car in pit lane or just exited (< 5s)
                    if other.pit_status != 0 or (now - self._pit_exit_time.get(i, 0) < 5.0):
                        is_rejected = True
                        reject_reason = "pit_lane"

                    # 2. Result status not active
                    elif other.result_status != 2:
                        is_rejected = True
                        reject_reason = "not_active"

                    # 3. Lapped / lapping
                    elif (my_lap_num < other.lap_num) or (candidate_dir == "loss" and other.lap_num > my_lap_num) or (track_len > 0 and abs(abs_gap - track_len) < 200):
                        is_rejected = True
                        reject_reason = "lapped_traffic"

                    # 4. Safety car or VSC
                    elif state.safety_car in (1, 2, 3) or (state.my_status and state.my_status.fia_flag in (1, 2, 3)):
                        is_rejected = True
                        reject_reason = "safety_car"

                    # 5. First 200m after race start
                    elif my_lap_num == 1 and (my_lap_dist < 200.0 or my_total < 200.0):
                        is_rejected = True
                        reject_reason = "race_start_chaos"

                    # 6. Other car driving wrong way
                    elif hasattr(state, "tel2") and state.tel2 and i < len(state.tel2):
                        o_tel2 = state.tel2[i]
                        if o_tel2 and getattr(o_tel2, "wrong_way", False):
                            is_rejected = True
                            reject_reason = "wrong_way"

                    if is_rejected:
                        log_detectors.info(
                            "Overtake candidate car %d, confirmed=False, reason=%s",
                            i, reject_reason
                        )
                    else:
                        # Stricken car check
                        is_stricken = False
                        if candidate_dir == "pass":
                            other_q = self._speed_history.get(i, deque())
                            recent_other = [s for t, s in other_q if now - t <= 3.0]
                            recent_me = [s for t, s in self._player_speed_history if now - t <= 3.0]
                            if recent_other and recent_me:
                                avg_other = float(np.mean(recent_other))
                                avg_me = float(np.mean(recent_me))
                                if avg_me > 30.0 and avg_other < 0.60 * avg_me:
                                    is_stricken = True

                        # Find matching corner/zone from TrackModel or BrakingZones
                        matched_corner: Corner | None = None
                        matched_zone: BrakingZone | None = None
                        loc = "straight"
                        side = "inside"

                        if self.track_model:
                            matched_corner = self.track_model.get_corner_at_dist(my_lap_dist)
                            if matched_corner is None:
                                # Check upcoming corner within 150m (braking zone entry)
                                up_c, up_dist = self.track_model.get_upcoming_corner(my_lap_dist, lookahead_m=150.0)
                                if up_c and up_dist <= 150.0:
                                    matched_corner = up_c
                                    loc = "braking_zone"
                            else:
                                if my_lap_dist < matched_corner.apex_dist_m:
                                    loc = "braking_zone" if my_lap_dist <= matched_corner.start_dist_m else "mid_corner"
                                else:
                                    loc = "exit"

                        # Fallback to zones from BrakingZoneDetector if no track_model corner
                        if matched_corner is None and zones:
                            for z in zones:
                                z_start = (z.start_dist_m - 150.0) % (track_len if track_len > 0 else 1e9)
                                z_end = (z.apex_dist_m + 100.0) % (track_len if track_len > 0 else 1e9)
                                if (z_start < z_end and z_start <= my_lap_dist <= z_end) or (z_start > z_end and (my_lap_dist >= z_start or my_lap_dist <= z_end)):
                                    matched_zone = z
                                    loc = "braking_zone"
                                    break

                        # Check relative side (inside vs outside)
                        if (matched_corner or matched_zone) and state.motion and p_idx < len(state.motion) and i < len(state.motion):
                            p_mot = state.motion[p_idx]
                            o_mot = state.motion[i]
                            if p_mot and o_mot:
                                c_dir = matched_corner.direction if matched_corner else matched_zone.direction
                                if self.track_model:
                                    p_lat = self.track_model.get_lateral_offset(p_mot.x, p_mot.z, my_lap_dist)
                                    o_lat = self.track_model.get_lateral_offset(o_mot.x, o_mot.z, other.lap_distance)
                                    if c_dir == "right":
                                        side = "outside" if p_lat < o_lat - 0.5 else "inside"
                                    else:
                                        side = "outside" if p_lat > o_lat + 0.5 else "inside"

                        # Check overtake mode in last 5s
                        ot_mode_active = any(act for t, act in self._overtake_mode_history if now - t <= 5.0)

                        cand = OvertakeCandidate(
                            car_idx=i,
                            direction=candidate_dir,
                            start_time=now,
                            initial_my_total=my_total,
                            initial_their_total=their_total,
                            is_stricken=is_stricken,
                            corner=matched_corner,
                            braking_zone=matched_zone,
                            location=loc,
                            side=side,
                            overtake_mode_active=ot_mode_active,
                            slipstream=(self._slipstream_duration.get(i, 0.0) >= 3.0),
                            other_warnings_start=getattr(other, "corner_cutting_warnings", 0),
                            other_invalid_start=getattr(other, "lap_invalid", False),
                            is_backmarker=is_backmarker,
                        )

                        # Motion gap and overlap at turn-in
                        if state.motion and p_idx < len(state.motion) and i < len(state.motion):
                            p_mot = state.motion[p_idx]
                            o_mot = state.motion[i]
                            if p_mot and o_mot:
                                dx = o_mot.x - p_mot.x
                                dy = o_mot.y - p_mot.y
                                dz = o_mot.z - p_mot.z
                                d_fwd = dx * p_mot.fwd_x + dy * p_mot.fwd_y + dz * p_mot.fwd_z
                                d_rt = dx * p_mot.right_x + dy * p_mot.right_y + dz * p_mot.right_z
                                cand.start_gap_m = d_fwd
                                cand.overlap_at_turn_in = (abs(d_fwd) < 3.0 and abs(d_rt) > 1.0)
                                cand.turn_in_recorded = True

                        if matched_corner:
                            cand.brake_vs_field_m = my_lap_dist - matched_corner.braking_start_median_m
                            cand.braking_delta_m = cand.brake_vs_field_m
                        elif matched_zone:
                            cand.braking_delta_m = my_lap_dist - matched_zone.ref_braking_start_m
                            cand.brake_vs_field_m = cand.braking_delta_m

                        self._candidates[i] = cand
                        c_name = matched_corner.name if matched_corner else (matched_zone.name if matched_zone else "None")
                        log_detectors.debug(
                            "Overtake candidate car %d started: dir=%s, corner=%s, gap=%.1fm",
                            i, candidate_dir, c_name, abs_gap
                        )

        # Process active candidates for confirmation
        confirm_time = getattr(self.config, "confirm_time_s", 2.0)
        confirm_dist = getattr(self.config, "confirm_distance_m", 80.0)
        prox_m = getattr(self.config, "proximity_m", 60.0)

        for c_idx in list(self._candidates.keys()):
            cand = self._candidates[c_idx]
            other = state.lap[c_idx] if c_idx < len(state.lap) else None
            if other is None:
                del self._candidates[c_idx]
                continue

            their_total = getattr(other, "total_distance", 0.0)
            dist_diff = my_total - their_total
            curr_sign = 1 if dist_diff > 0 else -1
            expected_sign = 1 if cand.direction == "pass" else -1

            if curr_sign != expected_sign:
                log_detectors.info(
                    "Overtake candidate car %d, confirmed=False, reason=side_by_side_flicker (elapsed=%.2fs)",
                    c_idx, now - cand.start_time
                )
                del self._candidates[c_idx]
                continue

            # Update live features while candidate is active
            if cand.corner or cand.braking_zone:
                cand.player_min_speed = min(cand.player_min_speed, my_speed)

                # Check ran deep: wheels off track
                if state.my_telemetry and hasattr(state.my_telemetry, "surface_type"):
                    if any(st in OFF_TRACK_SURFACES for st in state.my_telemetry.surface_type):
                        cand.ran_deep = True

                # Check victim reaction: opponent brake spike, throttle lift > 0.5, or steer swerve > 0.3
                if c_idx in self._opponent_inputs:
                    inp_q = self._opponent_inputs[c_idx]
                    recent_inps = [item for item in inp_q if now - item[0] <= 1.5]
                    if len(recent_inps) >= 2:
                        throttles = [item[2] for item in recent_inps]
                        steers = [item[3] for item in recent_inps]
                        brakes = [item[1] for item in recent_inps]
                        if (max(throttles) - min(throttles) > 0.5) or (max(steers) - min(steers) > 0.3) or (max(brakes) > 0.6 and min(brakes) < 0.2):
                            cand.victim_reaction = True

            elapsed = now - cand.start_time
            dist_travelled = abs(my_total - cand.initial_my_total)
            # Confirm if held for 2.0s OR 80m travelled
            if elapsed >= confirm_time or dist_travelled >= confirm_dist:
                del self._candidates[c_idx]
                record = self._confirm_overtake(state, c_idx, cand, now)
                signals.append(record)

        return signals

    def _classify_overtake(
        self,
        features: dict[str, Any],
        is_inside_corner: bool | None = None,
        corner_or_zone: Any | None = None,
        switchback: bool = False,
        is_stricken: bool = False,
        direction: str = "pass",
        side: str = "inside",
        overtake_mode_active: bool = False,
        location: str | None = None,
        **kwargs: Any,
    ) -> str:
        if location is None:
            location = features.get("location")
        if is_inside_corner is None:
            is_inside_corner = kwargs.get("is_inside_braking", True)
        if corner_or_zone is None and "zone" in kwargs:
            corner_or_zone = kwargs.get("zone")

        contact = bool(features.get("contact"))
        contact_severity = int(features.get("contact_severity", 1 if contact else 0))
        forced_off = bool(features.get("forced_off"))
        ran_deep = bool(features.get("ran_deep"))
        victim_reaction = bool(features.get("victim_reaction"))
        braking_delta = features.get("braking_delta_m", 0.0)
        start_gap = features.get("start_gap_m", 0.0)
        overlap = bool(features.get("overlap_at_turn_in"))

        if is_stricken:
            return "passed_stricken_car"
        if kwargs.get("is_backmarker") or kwargs.get("backmarker"):
            return "backmarker_pass"
        if switchback:
            return "overtake_switchback"

        # Pass logic (I overtook other car)
        if direction == "pass":
            # Overtake Mode pass on straight
            if overtake_mode_active and location == "straight" and not contact:
                return "overtake_mode_pass"

            # Clean pass on straight without overtake mode
            if location == "straight" and not contact:
                return "overtake_straight"

            if not is_inside_corner or corner_or_zone is None:
                return "clean"

            is_divebomb = (
                start_gap < getattr(self.config, "divebomb_start_gap_max_m", -10.0)
                or braking_delta > getattr(self.config, "divebomb_braking_delta_min_m", 25.0)
            ) and (ran_deep or forced_off or victim_reaction)

            if is_divebomb:
                return "divebomb_contact" if (contact and contact_severity >= 1) else "divebomb"

            is_brave = (
                (self.config.brave_braking_delta_min_m <= braking_delta <= self.config.brave_braking_delta_max_m)
                or (self.config.brave_start_gap_min_m <= start_gap <= self.config.brave_start_gap_max_m)
            ) and not contact and not forced_off and not ran_deep

            if side == "outside" and (is_brave or overlap or braking_delta < self.config.clean_braking_delta_max_m):
                return "around_the_outside"

            if is_brave:
                return "brave"
            elif (braking_delta < self.config.clean_braking_delta_max_m or overlap) and not contact and not forced_off:
                return "clean"
            else:
                return "divebomb_contact" if contact else "clean"

        # Lost position logic (Opponent overtook me)
        else:
            if is_inside_corner and corner_or_zone is None:
                return "fair_pass_against"

            # Check if opponent divebombed me
            other_divebomb = (
                start_gap < getattr(self.config, "divebomb_start_gap_max_m", -10.0)
                or braking_delta > getattr(self.config, "divebomb_braking_delta_min_m", 25.0)
            ) and (ran_deep or forced_off or victim_reaction)

            if other_divebomb:
                return "divebombed_by"
            elif forced_off:
                return "forced_off_by"
            else:
                return "fair_pass_against"

    def _confirm_overtake(
        self,
        state: RaceState,
        c_idx: int,
        cand: OvertakeCandidate,
        now: float,
    ) -> dict[str, Any]:
        """Calculates features, classifies move, and records overtake."""
        corner = getattr(cand, "corner", None)
        zone = getattr(cand, "braking_zone", None)
        other_name = state.name(c_idx).split()[-1] if state.name(c_idx) else f"Car {c_idx}"

        # 1. Contact check: COLL event with that car within +-2s (with severity)
        contact = False
        contact_sev = 0
        for ev in self._recent_collisions:
            ev_t = ev[0]
            v1, v2 = ev[1], ev[2]
            sev = ev[3] if len(ev) > 3 else 1
            if abs(now - ev_t) <= getattr(self.config, "contact_window_s", 2.0):
                if (v1 == c_idx and v2 == state.player) or (v2 == c_idx and v1 == state.player):
                    contact = True
                    contact_sev = max(contact_sev, sev)

        # 2. Forced off check
        forced_off = getattr(cand, "forced_off", False)
        cand_warnings = getattr(cand, "other_warnings_start", 0)
        cand_invalid = getattr(cand, "other_invalid_start", False)
        if state.lap and c_idx < len(state.lap) and state.lap[c_idx] is not None:
            o_lap = state.lap[c_idx]
            warn_inc = o_lap.corner_cutting_warnings > cand_warnings
            inv_changed = bool(o_lap.lap_invalid) and not cand_invalid
            if warn_inc or inv_changed:
                forced_off = True

        if state.tel and c_idx < len(state.tel) and state.tel[c_idx] is not None:
            o_tel = state.tel[c_idx]
            if hasattr(o_tel, "surface_type") and any(st in OFF_TRACK_SURFACES for st in o_tel.surface_type):
                forced_off = True

        # 3. Ran deep check
        ran_deep = getattr(cand, "ran_deep", False)
        player_min_spd = getattr(cand, "player_min_speed", 999.0)
        ref_min = getattr(corner, "field_median_min_speed_kph", None) if corner else (getattr(zone, "ref_min_speed_kph", None) if zone else None)
        if ref_min is not None and player_min_spd < 900.0:
            if player_min_spd > ref_min + getattr(self.config, "ran_deep_min_speed_delta_kph", 15.0):
                ran_deep = True

        # 4. Switchback check
        cand_dir = getattr(cand, "direction", "pass")
        switchback = any(
            now - r["timestamp"] <= 5.0
            and r["car_idx"] == c_idx
            and r["direction"] != cand_dir
            for r in self._recent_overtakes
        )

        braking_delta = getattr(cand, "braking_delta_m", None) or 0.0
        start_gap = getattr(cand, "start_gap_m", None) or 0.0
        brake_vs_field = getattr(cand, "brake_vs_field_m", None) or braking_delta
        overlap_at_turn_in = getattr(cand, "overlap_at_turn_in", False)
        victim_reaction = getattr(cand, "victim_reaction", False)
        cand_loc = getattr(cand, "location", "corner" if (corner or zone) else "straight")
        cand_side = getattr(cand, "side", "inside")
        overtake_mode = getattr(cand, "overtake_mode_active", False)
        slipstream = getattr(cand, "slipstream", False)
        is_stricken = getattr(cand, "is_stricken", False)

        corner_name = getattr(corner, "name", None) or getattr(zone, "name", None) or "Straight"

        features = {
            "braking_delta_m": round(braking_delta, 1),
            "brake_vs_field_m": round(brake_vs_field, 1),
            "start_gap_m": round(start_gap, 1),
            "overlap_at_turn_in": overlap_at_turn_in,
            "ran_deep": ran_deep,
            "contact": contact,
            "contact_severity": contact_sev,
            "forced_off": forced_off,
            "victim_reaction": victim_reaction,
            "switchback": switchback,
            "location": cand_loc,
            "side": cand_side,
            "overtake_mode": overtake_mode,
            "slipstream": slipstream,
            "corner_name": corner_name,
        }

        cls_name = self._classify_overtake(
            features=features,
            is_inside_corner=(corner is not None or zone is not None),
            corner_or_zone=corner or zone,
            switchback=switchback,
            is_stricken=is_stricken,
            direction=cand_dir,
            side=cand_side,
            overtake_mode_active=overtake_mode,
            location=cand_loc,
            is_backmarker=getattr(cand, "is_backmarker", False),
        )

        record = {
            "session_uid": state.session_uid,
            "lap_num": state.me.lap_num if state.me else 1,
            "timestamp": now,
            "car_idx": c_idx,
            "other_vehicle_idx": c_idx,
            "other_driver_name": other_name,
            "direction": cand.direction,
            "classification": cls_name,
            "braking_delta_m": features["braking_delta_m"],
            "start_gap_m": features["start_gap_m"],
            "overlap_at_turn_in": 1 if features["overlap_at_turn_in"] else 0,
            "ran_deep": 1 if features["ran_deep"] else 0,
            "contact": 1 if features["contact"] else 0,
            "forced_off": 1 if features["forced_off"] else 0,
            "switchback": 1 if features["switchback"] else 0,
            "corner_name": features["corner_name"],
            "location": features["location"],
            "side": features["side"],
            "raw_features": features,
            "position": getattr(state.me, "position", 0) if state.me else 0,
        }

        self._recent_overtakes.append(record)
        log_detectors.info(
            "Overtake confirmed: car %d (%s), dir=%s, class=%s, features=%s",
            c_idx, other_name, cand.direction, cls_name, features
        )

        if self.storage:
            self.storage.save_overtake(record)

        return record


class DefendWarningDetector:
    """Detects cars closing behind 300-150m before braking zones with inside-look detection (Prompt 21)."""

    def __init__(
        self,
        config: DefendConfig | None = None,
        braking_zones: BrakingZoneDetector | None = None,
        spotter: Spotter | None = None,
        track_model: TrackModel | None = None,
    ):
        self.config = config or DefendConfig()
        self.braking_zones = braking_zones or BrakingZoneDetector()
        self.spotter = spotter
        self.track_model = track_model

        # Rate limiting: (lap_num, corner_num)
        self._corner_lap_fired: set[tuple[int, int]] = set()
        self._last_call_time: float = -100.0

        # Gap and lateral history for car behind: car_idx -> deque of (time, d_forward, d_right)
        self._motion_history: dict[int, deque[tuple[float, float, float]]] = {}

    def set_track_model(self, model: TrackModel):
        self.track_model = model

    def update(self, state: RaceState, now: float | None = None) -> list[dict[str, Any]]:
        """Evaluates car-behind telemetry and triggers defend warnings."""
        if now is None:
            now = time.monotonic()

        # Overall 20s cooldown
        if now - self._last_call_time < self.config.cooldown_overall_s:
            return []

        # Never while spotter has an active overlap
        if self.spotter and self.spotter.state != SpotterState.CLEAR:
            return []

        p_idx = state.player
        if state.lap is None or p_idx >= len(state.lap) or state.motion is None or p_idx >= len(state.motion):
            return []

        me = state.lap[p_idx]
        p_mot = state.motion[p_idx]
        if me is None or p_mot is None:
            return []

        my_lap_dist = me.lap_distance
        my_lap_num = me.lap_num
        track_len = state.track_length

        # Find upcoming corner/braking zone 300-150m ahead
        target_name = "corner"
        target_dir = "right"
        target_num = 1

        found_target = False
        if self.track_model and self.track_model.corners:
            up_c, up_dist = self.track_model.get_upcoming_corner(my_lap_dist, lookahead_m=350.0)
            if up_c and self.config.zone_distance_min_m <= up_dist <= self.config.zone_distance_max_m:
                if (my_lap_num, up_c.number) not in self._corner_lap_fired:
                    target_name = up_c.name
                    target_dir = up_c.direction
                    target_num = up_c.number
                    found_target = True

        if not found_target:
            track_id = state.session.track_id if state.session else 0
            zones = self.braking_zones.get_zones(track_id)
            if zones and track_len > 0:
                for z in zones:
                    dist_to_start = (z.start_dist_m - my_lap_dist) % track_len
                    if self.config.zone_distance_min_m <= dist_to_start <= self.config.zone_distance_max_m:
                        if (my_lap_num, z.corner_number) not in self._corner_lap_fired:
                            target_name = z.name
                            target_dir = z.direction
                            target_num = z.corner_number
                            found_target = True
                            break

        if not found_target:
            return []

        # Check cars behind
        fwd_x, fwd_y, fwd_z = p_mot.fwd_x, p_mot.fwd_y, p_mot.fwd_z
        rt_x, rt_y, rt_z = p_mot.right_x, p_mot.right_y, p_mot.right_z

        num_cars = len(state.lap)
        for i in range(num_cars):
            if i == p_idx:
                continue
            other = state.lap[i]
            if other is None or other.result_status != 2 or other.pit_status != 0:
                continue
            if i >= len(state.motion) or state.motion[i] is None:
                continue

            o_mot = state.motion[i]
            dx = o_mot.x - p_mot.x
            dy = o_mot.y - p_mot.y
            dz = o_mot.z - p_mot.z
            d_fwd = dx * fwd_x + dy * fwd_y + dz * fwd_z
            d_rt = dx * rt_x + dy * rt_y + dz * rt_z

            if i not in self._motion_history:
                self._motion_history[i] = deque(maxlen=60)
            self._motion_history[i].append((now, d_fwd, d_rt))

            # Car behind check: within 15m or gap < 0.4s
            behind_ms = getattr(other, "gap_to_car_ahead_ms", None)
            is_close_meters = (-self.config.close_distance_m <= d_fwd <= 0.0)
            is_close_seconds = (other.position == me.position + 1 and behind_ms is not None and behind_ms < int(self.config.close_time_s * 1000))

            if is_close_meters or is_close_seconds:
                # Closing check: gap shrank over last ~1.0s
                q = self._motion_history[i]
                while q and now - q[0][0] > 3.0:
                    q.popleft()
                old_samples = [item for item in q if 0.7 <= now - item[0] <= 1.8]
                if not old_samples:
                    old_samples = [item for item in q if now - item[0] >= 0.7]
                if not old_samples:
                    continue
                old_time, old_fwd, old_rt = old_samples[-1]
                is_closing = (d_fwd > old_fwd + 0.2) or (behind_ms is not None)

                if not is_closing:
                    continue

                # Inside look check
                inside_look = False
                if -self.config.inside_distance_threshold_m <= d_fwd <= 0.0:
                    delta_rt = d_rt - old_rt
                    if target_dir == "right" and delta_rt > self.config.inside_move_threshold_m:
                        inside_look = True
                    elif target_dir == "left" and delta_rt < -self.config.inside_move_threshold_m:
                        inside_look = True

                self._corner_lap_fired.add((my_lap_num, target_num))
                self._last_call_time = now

                other_name = state.name(i).split()[-1] if state.name(i) else "he"
                info = {
                    "corner": target_name,
                    "inside_look": inside_look,
                    "driver": other_name,
                    "car_idx": i,
                    "d_forward": round(d_fwd, 1),
                    "d_right": round(d_rt, 1),
                }

                log_detectors.info(
                    "Defend warning candidate confirmed: corner=%s, inside_look=%s, driver=%s, d_fwd=%.1fm, d_rt=%.1fm",
                    target_name, inside_look, other_name, d_fwd, d_rt
                )
                return [info]

        return []
