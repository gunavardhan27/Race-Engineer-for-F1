"""Overtake detection, braking zone feature extraction, classification, and corner defend warnings.

Uses total_distance sign changes confirmed over 2.0s, extracts braking zone features
(braking_delta_m, start_gap_m, overlap_at_turn_in, ran_deep, contact, forced_off, switchback),
classifies into clean / brave / divebomb / divebomb_contact / switchback / passed_stricken_car,
and detects closing cars before braking zones with inside-look detection.
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

log = logging.getLogger("engineer.overtakes")
log_detectors = logging.getLogger("engineer.detectors")


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
            corner_names = self.knowledge.get_corner_names()

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
                # Apex is local speed minimum in braking zone or within 100m after
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

                # Direction from steer at apex
                apex_steer = float(steer[apex_idx]) if steer is not None and len(steer) > apex_idx else 0.0
                direction = "right" if apex_steer >= 0 else "left"

                c_name = f"Turn {corner_counter}"
                if corner_counter - 1 < len(corner_names):
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
    braking_zone: BrakingZone | None = None
    braking_delta_m: float | None = None
    start_gap_m: float | None = None
    overlap_at_turn_in: bool = False
    player_min_speed: float = 999.0
    player_exit_speed: float | None = None
    other_pre_apex_speed: float | None = None
    other_exit_speed: float | None = None
    other_warnings_start: int = 0
    other_invalid_start: bool = False
    turn_in_recorded: bool = False


class OvertakeDetector:
    """Deterministic telemetry-based overtake detection and classification."""

    def __init__(
        self,
        config: OvertakeConfig | None = None,
        storage: Any | None = None,
        braking_zones: BrakingZoneDetector | None = None,
    ):
        self.config = config or OvertakeConfig()
        self.storage = storage
        self.braking_zones = braking_zones or BrakingZoneDetector()

        # Sign tracking: car_idx -> last known sign of (my_total - their_total)
        self._prev_signs: dict[int, int] = {}
        self._prev_pit_status: dict[int, int] = {}
        self._pit_exit_time: dict[int, float] = {}

        # Active candidates: car_idx -> OvertakeCandidate
        self._candidates: dict[int, OvertakeCandidate] = {}

        # Collision events: deque of (time, v1, v2)
        self._recent_collisions: deque[tuple[float, int, int]] = deque(maxlen=50)

        # Recent confirmed overtakes: deque of (time, car_idx, direction) for switchback check
        self._recent_overtakes: deque[dict[str, Any]] = deque(maxlen=50)

        # Speed history for stricken car detection: car_idx -> deque of (time, speed)
        self._speed_history: dict[int, deque[tuple[float, float]]] = {}
        self._player_speed_history: deque[tuple[float, float]] = deque(maxlen=120)

    def on_collision(self, v1: int, v2: int, now: float | None = None):
        """Records COLL event for contact feature matching."""
        if now is None:
            now = time.monotonic()
        self._recent_collisions.append((now, v1, v2))

    def update(self, state: RaceState, now: float | None = None) -> list[tuple[str, dict[str, Any]]]:
        """Evaluates live telemetry and returns confirmed overtake/loss signals."""
        if now is None:
            now = time.monotonic()

        signals: list[tuple[str, dict[str, Any]]] = []
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

        track_id = state.session.track_id if state.session else 0
        track_len = state.track_length
        zones = self.braking_zones.get_zones(track_id)

        # Clean old speed history
        cutoff = now - 4.0
        for c_idx in list(self._speed_history.keys()):
            q = self._speed_history[c_idx]
            while q and q[0][0] < cutoff:
                q.popleft()

        # Update opponent pit status tracking and speed history
        for i, other in enumerate(state.lap):
            if i == p_idx or other is None:
                continue

            # Track pit status
            old_pit = self._prev_pit_status.get(i, 0)
            if old_pit != 0 and other.pit_status == 0:
                self._pit_exit_time[i] = now
            self._prev_pit_status[i] = other.pit_status

            # Approximate other car speed from Motion if available
            other_speed = 0.0
            if state.motion and i < len(state.motion) and state.motion[i] is not None:
                mot = state.motion[i]
                other_speed = math.sqrt(mot.vx ** 2 + mot.vy ** 2 + mot.vz ** 2) * 3.6
            if i not in self._speed_history:
                self._speed_history[i] = deque(maxlen=120)
            self._speed_history[i].append((now, other_speed))

            # Primary signal: total_distance difference
            their_total = getattr(other, "total_distance", 0.0)
            dist_diff = my_total - their_total
            abs_gap = abs(dist_diff)

            # Check if within proximity (50m)
            if abs_gap < self.config.proximity_m:
                sign = 1 if dist_diff > 0 else -1
                prev_sign = self._prev_signs.get(i)
                self._prev_signs[i] = sign

                if prev_sign is not None and sign != prev_sign:
                    # Sign change detected! Candidate pass or loss
                    candidate_dir = "pass" if sign == 1 else "loss"

                    # Evaluate filters
                    is_rejected = False
                    reject_reason = ""

                    # 1. Other car in pit lane or just exited (< 5s)
                    if other.pit_status != 0 or (now - self._pit_exit_time.get(i, 0) < 5.0):
                        is_rejected = True
                        reject_reason = "pit_lane"

                    # 2. Result status not active (2)
                    elif other.result_status != 2:
                        is_rejected = True
                        reject_reason = "not_active"

                    # 3. Lapped / lapping
                    elif abs(my_lap_num - other.lap_num) >= 1 or (track_len > 0 and abs(abs_gap - track_len) < 200):
                        is_rejected = True
                        reject_reason = "lapped_traffic"

                    # 4. Safety car or VSC
                    elif state.safety_car in (1, 2, 3) or (state.my_status and state.my_status.fia_flag in (1, 2, 3)):
                        is_rejected = True
                        reject_reason = "safety_car"

                    # 5. First 200m after race start (grid chaos handled by good_start/bad_start)
                    elif my_lap_num == 1 and (my_lap_dist < 200.0 or my_total < 200.0):
                        is_rejected = True
                        reject_reason = "race_start_chaos"

                    if is_rejected:
                        log_detectors.info(
                            "Overtake candidate car %d, confirmed=False, reason=%s",
                            i, reject_reason
                        )
                    else:
                        # Check stricken car: other car speed < 60% of player speed for previous 3s
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

                        # Find matching braking zone
                        matched_zone = None
                        for z in zones:
                            # 150m before braking start to 100m after apex
                            z_start = (z.start_dist_m - 150.0) % (track_len if track_len > 0 else 1e9)
                            z_end = (z.apex_dist_m + 100.0) % (track_len if track_len > 0 else 1e9)
                            if z_start < z_end:
                                if z_start <= my_lap_dist <= z_end:
                                    matched_zone = z
                                    break
                            else:  # Zone wraps around start/finish line
                                if my_lap_dist >= z_start or my_lap_dist <= z_end:
                                    matched_zone = z
                                    break

                        # Start candidate
                        cand = OvertakeCandidate(
                            car_idx=i,
                            direction=candidate_dir,
                            start_time=now,
                            initial_my_total=my_total,
                            initial_their_total=their_total,
                            is_stricken=is_stricken,
                            braking_zone=matched_zone,
                            other_warnings_start=getattr(other, "corner_cutting_warnings", 0),
                            other_invalid_start=getattr(other, "lap_invalid", False),
                        )

                        # Extract relative motion features at candidate start
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

                        if matched_zone:
                            cand.braking_delta_m = my_lap_dist - matched_zone.ref_braking_start_m

                        self._candidates[i] = cand
                        log_detectors.debug(
                            "Overtake candidate car %d started: dir=%s, zone=%s, gap=%.1fm",
                            i, candidate_dir, matched_zone.name if matched_zone else "None", abs_gap
                        )

        # Process active candidates
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

            # Check if sign held
            if curr_sign != expected_sign:
                # Sign flipped back: side-by-side flicker
                log_detectors.info(
                    "Overtake candidate car %d, confirmed=False, reason=side_by_side_flicker (elapsed=%.2fs)",
                    c_idx, now - cand.start_time
                )
                del self._candidates[c_idx]
                continue

            # Update features while in zone
            if cand.braking_zone:
                cand.player_min_speed = min(cand.player_min_speed, my_speed)
                # Check turn-in overlap if not recorded yet
                if not cand.turn_in_recorded and state.motion and p_idx < len(state.motion) and c_idx < len(state.motion):
                    p_mot = state.motion[p_idx]
                    o_mot = state.motion[c_idx]
                    if p_mot and o_mot:
                        dx = o_mot.x - p_mot.x
                        dy = o_mot.y - p_mot.y
                        dz = o_mot.z - p_mot.z
                        d_fwd = dx * p_mot.fwd_x + dy * p_mot.fwd_y + dz * p_mot.fwd_z
                        d_rt = dx * p_mot.right_x + dy * p_mot.right_y + dz * p_mot.right_z
                        if abs(d_fwd) < 3.0 and abs(d_rt) > 1.0:
                            cand.overlap_at_turn_in = True
                            cand.turn_in_recorded = True

            # Confirm candidate if held for 2.0s and gap < 50m
            elapsed = now - cand.start_time
            if elapsed >= self.config.confirm_time_s:
                del self._candidates[c_idx]
                record = self._confirm_overtake(state, c_idx, cand, now)
                signals.append(record)

        return signals

    def _classify_overtake(
        self,
        features: dict[str, Any],
        is_inside_braking: bool = True,
        zone: BrakingZone | None = None,
        switchback: bool = False,
        is_stricken: bool = False,
    ) -> str:
        contact = bool(features.get("contact"))
        forced_off = bool(features.get("forced_off"))
        ran_deep = bool(features.get("ran_deep"))
        braking_delta = features.get("braking_delta_m", 0.0)
        start_gap = features.get("start_gap_m", 0.0)
        overlap = bool(features.get("overlap_at_turn_in"))

        if is_stricken:
            return "passed_stricken_car"
        if switchback:
            return "overtake_switchback"
        if not is_inside_braking or zone is None:
            return "clean"

        is_divebomb_features = (
            start_gap < self.config.divebomb_start_gap_max_m
            or braking_delta > self.config.divebomb_braking_delta_min_m
        ) and (ran_deep or forced_off)

        if is_divebomb_features:
            return "divebomb_contact" if contact else "divebomb"
        elif (
            (self.config.brave_braking_delta_min_m <= braking_delta <= self.config.brave_braking_delta_max_m)
            or (self.config.brave_start_gap_min_m <= start_gap <= self.config.brave_start_gap_max_m)
        ) and not contact and not forced_off:
            return "brave"
        elif (braking_delta < self.config.clean_braking_delta_max_m or overlap) and not contact and not forced_off:
            return "clean"
        else:
            return "divebomb_contact" if contact else "clean"

    def _confirm_overtake(
        self,
        state: RaceState,
        c_idx: int,
        cand: OvertakeCandidate,
        now: float,
    ) -> dict[str, Any]:
        """Calculates features, classifies move, and records overtake."""
        # Find zone if any
        zone = cand.braking_zone
        other_name = state.name(c_idx).split()[-1] if state.name(c_idx) else f"Car {c_idx}"

        # 1. Contact check: was there a COLL event with that car within +-2s?
        contact = any(
            abs(now - ev_t) <= self.config.contact_window_s
            and ((v1 == c_idx and v2 == state.player) or (v2 == c_idx and v1 == state.player))
            for ev_t, v1, v2 in self._recent_collisions
        )

        # 2. Forced off check:
        forced_off = False
        if state.lap and c_idx < len(state.lap) and state.lap[c_idx] is not None:
            o_lap = state.lap[c_idx]
            warn_inc = o_lap.corner_cutting_warnings > cand.other_warnings_start
            inv_changed = bool(o_lap.lap_invalid) and not cand.other_invalid_start
            speed_drop = False
            if cand.other_pre_apex_speed is not None and cand.other_exit_speed is not None:
                speed_drop = (cand.other_pre_apex_speed - cand.other_exit_speed) > self.config.forced_off_speed_drop_kph
            forced_off = warn_inc or inv_changed or speed_drop

        # 3. Ran deep check:
        ran_deep = False
        if zone is not None:
            min_spd = cand.player_min_speed if cand.player_min_speed < 990.0 else (state.my_telemetry.speed_kph if state.my_telemetry else 0.0)
            exit_spd = cand.player_exit_speed if cand.player_exit_speed is not None else min_spd
            if (min_spd > zone.ref_min_speed_kph + self.config.ran_deep_min_speed_delta_kph and
                exit_spd < zone.ref_exit_speed_kph - self.config.ran_deep_exit_speed_delta_kph):
                ran_deep = True

        # 4. Switchback check: was this driver recently passed/passed us within 5s?
        switchback = any(
            now - r["timestamp"] <= 5.0
            and r["car_idx"] == c_idx
            and r["direction"] != cand.direction
            for r in self._recent_overtakes
        )

        braking_delta = cand.braking_delta_m if cand.braking_delta_m is not None else 0.0
        start_gap = cand.start_gap_m if cand.start_gap_m is not None else 0.0

        features = {
            "braking_delta_m": round(braking_delta, 1),
            "start_gap_m": round(start_gap, 1),
            "overlap_at_turn_in": cand.overlap_at_turn_in,
            "ran_deep": ran_deep,
            "contact": contact,
            "forced_off": forced_off,
            "switchback": switchback,
            "corner_name": zone.name if zone else "Straight",
        }

        cls_name = self._classify_overtake(
            features,
            is_inside_braking=(zone is not None),
            zone=zone,
            switchback=switchback,
            is_stricken=cand.is_stricken,
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
            "raw_features": features,
            "position": state.me.position if state.me else 0,
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
    """Detects cars closing behind 300-150m before braking zones with inside-look detection."""

    def __init__(
        self,
        config: DefendConfig | None = None,
        braking_zones: BrakingZoneDetector | None = None,
        spotter: Spotter | None = None,
    ):
        self.config = config or DefendConfig()
        self.braking_zones = braking_zones or BrakingZoneDetector()
        self.spotter = spotter

        # Rate limiting: (lap_num, corner_num)
        self._corner_lap_fired: set[tuple[int, int]] = set()
        self._last_call_time: float = -100.0

        # Gap and lateral history for car behind: car_idx -> deque of (time, d_forward, d_right)
        self._motion_history: dict[int, deque[tuple[float, float, float]]] = {}

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
        track_id = state.session.track_id if state.session else 0
        track_len = state.track_length
        zones = self.braking_zones.get_zones(track_id)
        if not zones or track_len <= 0:
            return []

        # Find upcoming braking zone 300-150m ahead
        target_zone: BrakingZone | None = None
        for z in zones:
            dist_to_start = (z.start_dist_m - my_lap_dist) % track_len
            if self.config.zone_distance_min_m <= dist_to_start <= self.config.zone_distance_max_m:
                if (my_lap_num, z.corner_number) not in self._corner_lap_fired:
                    target_zone = z
                    break

        if target_zone is None:
            return []

        # Check cars behind
        fwd_x, fwd_y, fwd_z = p_mot.fwd_x, p_mot.fwd_y, p_mot.fwd_z
        rt_x, rt_y, rt_z = p_mot.right_x, p_mot.right_y, p_mot.right_z

        for i, other in enumerate(state.lap):
            if i == p_idx or other is None:
                continue
            if other.result_status != 2 or other.pit_status != 0:
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

            # Car behind check: within 15m (d_forward between -15 and 0) or gap < 0.4s
            behind_ms = getattr(other, "gap_to_car_ahead_ms", None)
            is_close_meters = (-self.config.close_distance_m <= d_fwd <= 0.0)
            is_close_seconds = (other.position == me.position + 1 and behind_ms is not None and behind_ms < int(self.config.close_time_s * 1000))

            if is_close_meters or is_close_seconds:
                # Closing check: gap shrank over last 1.0s
                q = self._motion_history[i]
                old_samples = [item for item in q if now - item[0] >= 0.8]
                if not old_samples:
                    continue
                old_time, old_fwd, old_rt = old_samples[0]
                # Closer means d_fwd increased towards 0 (e.g. from -12m to -7m)
                is_closing = (d_fwd > old_fwd + 0.2) or (behind_ms is not None)

                if not is_closing:
                    continue

                # Inside look check:
                # If d_right moves toward inside of upcoming corner by > 1m while within 10m
                inside_look = False
                if -self.config.inside_distance_threshold_m <= d_fwd <= 0.0:
                    delta_rt = d_rt - old_rt
                    if target_zone.direction == "right" and delta_rt > self.config.inside_move_threshold_m:
                        inside_look = True
                    elif target_zone.direction == "left" and delta_rt < -self.config.inside_move_threshold_m:
                        inside_look = True

                # Trigger defend warning!
                self._corner_lap_fired.add((my_lap_num, target_zone.corner_number))
                self._last_call_time = now

                other_name = state.name(i).split()[-1] if state.name(i) else "he"
                info = {
                    "corner": target_zone.name,
                    "inside_look": inside_look,
                    "driver": other_name,
                    "car_idx": i,
                    "d_forward": round(d_fwd, 1),
                    "d_right": round(d_rt, 1),
                }

                log_detectors.info(
                    "Defend warning candidate confirmed: corner=%s, inside_look=%s, driver=%s, d_fwd=%.1fm, d_rt=%.1fm",
                    target_zone.name, inside_look, other_name, d_fwd, d_rt
                )
                return [info]

        return []
