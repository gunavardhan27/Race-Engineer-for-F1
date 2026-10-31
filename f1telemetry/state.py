"""Live race state built from parsed packets, plus the derived numbers an engineer uses."""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass

from . import packets
from .packets import PacketId, udp_action
from setups import compute_setup_fingerprint

log = logging.getLogger("engineer.state")
log_detectors = logging.getLogger("engineer.detectors")

TYRES = ("rear left", "rear right", "front left", "front right")   # F1 wheel order
COMPOUNDS = {16: "softs", 17: "mediums", 18: "hards", 7: "inters", 8: "wets"}
WEATHER = {0: "clear", 1: "light cloud", 2: "overcast", 3: "light rain", 4: "heavy rain", 5: "storm"}
SAFETY_CAR = {0: None, 1: "safety car", 2: "virtual safety car", 3: "formation lap"}
TRACKS = {0: "Melbourne", 2: "Shanghai", 3: "Bahrain", 4: "Barcelona", 5: "Monaco", 6: "Montreal",
          7: "Silverstone", 9: "Hungaroring", 10: "Spa", 11: "Monza", 12: "Singapore", 13: "Suzuka",
          14: "Abu Dhabi", 15: "Austin", 16: "Interlagos", 17: "Red Bull Ring", 19: "Mexico City",
          20: "Baku", 26: "Zandvoort", 27: "Imola", 29: "Jeddah", 30: "Miami", 31: "Las Vegas",
          32: "Losail", 39: "Silverstone reverse", 40: "Red Bull Ring reverse", 41: "Zandvoort reverse"}
ERS_MAX_J = 4_000_000
RUNNING = {2, 3}
SESSION_TYPES = {
    0: "Unknown",
    1: "Practice 1",
    2: "Practice 2",
    3: "Practice 3",
    4: "Short Practice",
    5: "Qualifying 1",
    6: "Qualifying 2",
    7: "Qualifying 3",
    8: "Short Qualifying",
    9: "One-Shot Qualifying",
    10: "Race",
    11: "Race 2",
    12: "Race 3",
    13: "Time Trial",
}


def _s(ms):
    return None if ms is None else round(ms / 1000, 2)


@dataclass(slots=True)
class LapRecord:
    """Snapshot taken as the player crosses the line."""
    lap: int
    lap_time_ms: int
    invalid: bool
    position: int
    ahead_idx: int | None
    gap_ahead_ms: int | None
    behind_idx: int | None
    gap_behind_ms: int | None
    fuel_kg: float | None
    tyre_wear: tuple | None
    stint: int
    sector1_ms: int = 0
    sector2_ms: int = 0
    sector3_ms: int = 0
    total_warnings: int = 0
    corner_cutting_warnings: int = 0
    grid_position: int = 0
    traffic_pct: float = 0.0
    had_safety_car: bool = False
    new_damage: bool = False
    pit_status: int = 0
    weather_changed: bool = False
    blunder_moment: str | None = None
    excess_deg: bool = False
    car_idx: int = 0

    @property
    def lap_time_s(self) -> float:
        return round(self.lap_time_ms / 1000.0, 3) if self.lap_time_ms else 0.0


class RaceState:
    def __init__(self, ptt_action: int = 1, history: int = 120):
        self.ptt_mask = udp_action(ptt_action)
        self._history = history
        self.last_packet_time = 0.0
        self._reset(None)

    def _reset(self, uid):
        self.session_uid = uid
        self.player = 0
        self.session = None
        self.lap = self.tel = self.status = self.damage = self.motion = self.setups = None
        self.tel2 = None
        self.tyre_sets = None
        self.lap_positions = None
        self._wrong_way_active = False
        self._overtake_available = False
        self.current_setup_id: str | int | None = None
        self.current_setup_fingerprint: str | None = None
        self.best_setup_info: dict | None = None
        self.names: dict[int, str] = {}
        self.participants: dict[int, packets.Participant] = {}
        self.last_packet_time = 0.0
        self.session_time = 0.0
        self.laps: deque[LapRecord] = deque(maxlen=self._history)
        self._lap_num = None
        self._lap_invalid = False
        self._corner_cutting_warnings = None
        self._buttons = 0
        self.personal_best = None
        self.last_lap_vs_best = None
        self.my_theoretical_best = None
        self.biggest_loss_segment = None
        self.fastest_sectors = None
        self.final_classification = None

        # Pace, qualifying and time trial tracking
        self.time_trial: packets.TimeTrialData | None = None
        self.best_sectors_session = [0, 0, 0]  # S1, S2, S3 in ms across all cars
        self.best_laps_all_cars: dict[int, int] = {}  # car_idx -> best lap time ms
        self.speed_traps_all_cars: dict[int, float] = {}  # car_idx -> max speed kph
        self.last_lap_vs_previous: dict | None = None
        self.expected_performance: dict = {"position": None, "basis": "none", "confidence": 0.0}

        # Battle pace, lap history and retirements
        self.car_laps_history: dict[int, list[dict]] = {}
        self._last_seen_lap_per_car: dict[int, int] = {}
        self.retirements: list[dict] = []
        self.retired_car_indices: set[int] = set()

        # Per-lap tracking accumulators
        self._lap_start_time = time.monotonic()
        self._last_traffic_sample_time = time.monotonic()
        self._lap_traffic_time_s = 0.0
        self._had_safety_car_this_lap = False
        self._new_damage_this_lap = False
        self._damage_at_lap_start: dict = {}
        self._weather_at_lap_start: int | None = None
        self._weather_changed_this_lap = False
        self._blunder_moment_this_lap: str | None = None
        self._player_s1_evaluated = False
        self._player_s2_evaluated = False
        self._player_prev_result_status = 0

    # ---- ingestion -------------------------------------------------------
    def update(self, h: packets.Header, data) -> list[tuple]:
        """Apply one parsed packet. Returns signals: ('lap', rec), ('ptt', bool),
        ('event', Event), ('damage',), ('new_session',), ('flashback', lap_num),
        ('lap_invalidated',), ('track_limits_warning', n), ('penalty', Event),
        ('final_classification', data), ('finished', info)."""
        signals = []
        if h.session_uid == 0:
            log_detectors.debug("Ignoring packet with session_uid == 0")
            return signals

        self.last_packet_time = time.monotonic()
        self.session_time = getattr(h, "session_time", 0.0)
        if self.session_uid is None:
            self.session_uid = h.session_uid
        elif h.session_uid != self.session_uid:
            log.info("Session reset: old_uid=%s, new_uid=%s", self.session_uid, h.session_uid)
            log_detectors.info("Session reset: old_uid=%s, new_uid=%s", self.session_uid, h.session_uid)
            self._reset(h.session_uid)
            self.last_packet_time = time.monotonic()
            signals.append(("new_session", h.session_uid))
        self.player = h.player_idx
        pid = h.packet_id
        if pid == PacketId.MOTION:
            self.motion = data
        elif pid == PacketId.LAP_DATA:
            self.lap = data
            if data:
                if self.player < len(data):
                    me_c = data[self.player]
                    prev_status = getattr(self, "_player_prev_result_status", 0)
                    self._player_prev_result_status = me_c.result_status
                    if prev_status == 2 and me_c.result_status == 3:
                        pos = me_c.position
                        gap_ahead_ms = me_c.gap_to_car_ahead_ms
                        gap_behind_ms = None
                        behind_idx = None
                        for ci, c in enumerate(data):
                            if c.position == pos + 1:
                                gap_behind_ms = c.gap_to_car_ahead_ms
                                behind_idx = ci
                                break
                        finish_info = {
                            "position": pos,
                            "gap_ahead_ms": gap_ahead_ms,
                            "gap_behind_ms": gap_behind_ms,
                            "penalties_s": getattr(me_c, "penalties_s", 0),
                            "behind_idx": behind_idx,
                            "session_uid": self.session_uid,
                        }
                        log_detectors.info(
                            "Finish detected: status 2->3, pos=%d, gap_ahead_ms=%s, gap_behind_ms=%s, penalties_s=%d",
                            pos, gap_ahead_ms, gap_behind_ms, finish_info["penalties_s"]
                        )
                        signals.append(("finished", finish_info))
                for i, c in enumerate(data):
                    prev_l = self._last_seen_lap_per_car.get(i, 0)
                    curr_l = getattr(c, "current_lap_num", 0)
                    last_ms = getattr(c, "last_lap_ms", 0)
                    if curr_l > prev_l and prev_l > 0 and last_ms > 0:
                        inv = bool(getattr(c, "lap_invalid", False))
                        self.car_laps_history.setdefault(i, []).append({
                            "lap_num": prev_l,
                            "lap_time_ms": last_ms,
                            "lap_time_s": round(last_ms / 1000.0, 3),
                            "valid": not inv,
                        })
                    self._last_seen_lap_per_car[i] = curr_l

                    # DNF / retirement check on status transition
                    status = getattr(c, "result_status", 0)
                    if status in (4, 7) and i not in self.retired_car_indices:
                        self.retired_car_indices.add(i)
                        d_name = self.name(i)
                        ret_rec = {
                            "car_idx": i,
                            "name": d_name,
                            "position": getattr(c, "position", 0),
                            "lap": curr_l,
                            "reason": "DNF" if status == 4 else "Retired",
                            "timestamp": time.monotonic(),
                        }
                        self.retirements.append(ret_rec)
                        signals.append(("retirement", ret_rec))

                    if hasattr(c, "speed_trap_kph") and c.speed_trap_kph > self.speed_traps_all_cars.get(i, 0.0):
                        self.speed_traps_all_cars[i] = c.speed_trap_kph
                    if c.last_lap_ms > 0 and not getattr(c, "lap_invalid", False):
                        prev_best = self.best_laps_all_cars.get(i)
                        if prev_best is None or c.last_lap_ms < prev_best:
                            self.best_laps_all_cars[i] = c.last_lap_ms
                            if i == self.player:
                                self.personal_best = round(c.last_lap_ms / 1000.0, 3)
                    if c.sector1_ms > 0 and not getattr(c, "lap_invalid", False):
                        if self.best_sectors_session[0] == 0 or c.sector1_ms < self.best_sectors_session[0]:
                            self.best_sectors_session[0] = c.sector1_ms
                    if c.sector2_ms > 0 and not getattr(c, "lap_invalid", False):
                        if self.best_sectors_session[1] == 0 or c.sector2_ms < self.best_sectors_session[1]:
                            self.best_sectors_session[1] = c.sector2_ms

                now = time.monotonic()
                dt = now - self._last_traffic_sample_time
                self._last_traffic_sample_time = now
                if dt > 0 and self.player < len(data):
                    me_c = data[self.player]
                    if me_c.position > 1 and me_c.gap_to_car_ahead_ms is not None:
                        if 0 <= me_c.gap_to_car_ahead_ms <= 1500:
                            self._lap_traffic_time_s += dt

            signals += self._check_lap()
        elif pid == PacketId.CAR_TELEMETRY:
            self.tel = data
        elif pid == PacketId.CAR_STATUS:
            self.status = data
        elif pid == PacketId.CAR_DAMAGE:
            self.damage = data
            signals.append(("damage",))
            if data and self.player < len(data):
                parts = self.damage_parts()
                if any(parts.get(k, 0) > self._damage_at_lap_start.get(k, 0) for k in parts):
                    self._new_damage_this_lap = True
        elif pid == PacketId.SESSION:
            prev_track_id = self.session.track_id if self.session else None
            self.session = data
            if data:
                if prev_track_id is None or data.track_id != prev_track_id:
                    signals.append(("track_change", data.track_id))
                if data.safety_car in (1, 2):
                    self._had_safety_car_this_lap = True
                if self._weather_at_lap_start is None:
                    self._weather_at_lap_start = data.weather
                elif data.weather != self._weather_at_lap_start:
                    self._weather_changed_this_lap = True
        elif pid == PacketId.PARTICIPANTS:
            self.participants = {i: p for i, p in enumerate(data) if p.name}
            self.names = {i: p.name for i, p in enumerate(data) if p.name}
        elif pid == PacketId.CAR_SETUPS:
            self.setups = data
            my = self.my_setup
            if my is not None:
                fp = compute_setup_fingerprint(my.to_dict())
                if fp != self.current_setup_fingerprint:
                    self.current_setup_fingerprint = fp
                    signals.append(("setup_change", my, fp))
        elif pid == PacketId.FINAL_CLASSIFICATION:
            self.final_classification = data
            signals.append(("final_classification", data))
        elif pid == PacketId.TIME_TRIAL:
            self.time_trial = data
            signals.append(("time_trial", data))
        elif pid == PacketId.CAR_TELEMETRY2:
            self.tel2 = data
            me2 = self.my_tel2
            if me2 is not None:
                if me2.wrong_way:
                    if not getattr(self, "_wrong_way_active", False):
                        self._wrong_way_active = True
                        signals.append(("wrong_way",))
                else:
                    self._wrong_way_active = False

                was_available = getattr(self, "_overtake_available", False)
                if me2.overtake_available and not was_available:
                    signals.append(("overtake_available", me2.overtake_in_m))
                self._overtake_available = me2.overtake_available
        elif pid == PacketId.TYRE_SETS:
            self.tyre_sets = data
        elif pid == PacketId.LAP_POSITIONS:
            self.lap_positions = data
        elif pid == PacketId.EVENT:
            if data.code == "BUTN":
                was = bool(self._buttons & self.ptt_mask)
                now = bool(data.buttons & self.ptt_mask)
                self._buttons = data.buttons
                if now != was:
                    signals.append(("ptt", now))
            elif data.code == "PENA" and data.vehicle_idx == self.player:
                signals.append(("penalty", data))
                self._blunder_moment_this_lap = "penalty"
            elif data.code == "COLL":
                v1 = getattr(data, "vehicle_1_idx", getattr(data, "vehicle_idx", None))
                v2 = getattr(data, "vehicle_2_idx", getattr(data, "other_vehicle_idx", None))
                if v1 == self.player or v2 == self.player:
                    self._blunder_moment_this_lap = "contact"
                signals.append(("event", data))
            elif data.code == "RTMT":
                car_i = getattr(data, "vehicle_idx", None)
                if car_i is not None and car_i not in self.retired_car_indices:
                    self.retired_car_indices.add(car_i)
                    d_name = self.name(car_i)
                    pos = getattr(self.lap[car_i], "position", 0) if self.lap and car_i < len(self.lap) else 0
                    lap_num = getattr(self.lap[car_i], "current_lap_num", 0) if self.lap and car_i < len(self.lap) else 0
                    ret_rec = {
                        "car_idx": car_i,
                        "name": d_name,
                        "position": pos,
                        "lap": lap_num,
                        "reason": "Retired",
                        "timestamp": time.monotonic(),
                    }
                    self.retirements.append(ret_rec)
                    signals.append(("retirement", ret_rec))
                signals.append(("event", data))
            else:
                signals.append(("event", data))
        return signals

    def _check_lap(self):
        me = self.me
        if me is None:
            return []
        if self._lap_num is None or me.lap_num < self._lap_num:   # first packet, flashback or restart
            was_flashback = (self._lap_num is not None and me.lap_num < self._lap_num)
            while self.laps and self.laps[-1].lap >= me.lap_num:
                self.laps.pop()
            self._lap_num, self._lap_invalid = me.lap_num, me.lap_invalid
            self._corner_cutting_warnings = me.corner_cutting_warnings
            if was_flashback:
                return [("flashback", me.lap_num)]
            return []
        if me.lap_num == self._lap_num:
            signals = []
            if not self._lap_invalid and me.lap_invalid:
                self._lap_invalid = True
                signals.append(("lap_invalidated",))
            if self._corner_cutting_warnings is None:
                self._corner_cutting_warnings = me.corner_cutting_warnings
            elif me.corner_cutting_warnings < self._corner_cutting_warnings:
                self._corner_cutting_warnings = me.corner_cutting_warnings
            elif me.corner_cutting_warnings > self._corner_cutting_warnings:
                signals.append(("track_limits_warning", me.corner_cutting_warnings))
                self._corner_cutting_warnings = me.corner_cutting_warnings

            # Purple sector checks during lap
            if not self._lap_invalid:
                if me.sector1_ms > 0 and not self._player_s1_evaluated:
                    self._player_s1_evaluated = True
                    if self.best_sectors_session[0] == 0 or me.sector1_ms <= self.best_sectors_session[0]:
                        self.best_sectors_session[0] = me.sector1_ms
                        signals.append(("purple_sector", 1))
                if me.sector2_ms > 0 and not self._player_s2_evaluated:
                    self._player_s2_evaluated = True
                    if self.best_sectors_session[1] == 0 or me.sector2_ms <= self.best_sectors_session[1]:
                        self.best_sectors_session[1] = me.sector2_ms
                        signals.append(("purple_sector", 2))
            return signals

        ahead_idx, gap_ahead = self.gap_ahead()
        behind_idx, gap_behind = self.gap_behind()
        st, dmg = self.my_status, self.my_damage
        s1 = me.sector1_ms
        s2 = me.sector2_ms
        s3 = max(0, me.last_lap_ms - s1 - s2) if (me.last_lap_ms > 0 and s1 > 0 and s2 > 0) else 0

        now = time.monotonic()
        lap_duration = max(1.0, (now - self._lap_start_time))
        traffic_pct = min(1.0, self._lap_traffic_time_s / lap_duration)

        excess_deg = False
        if dmg and dmg.tyre_wear and self.laps:
            wear_rate = self.wear_per_lap()
            prev_lap = self.laps[-1]
            if wear_rate and prev_lap.tyre_wear:
                current_wear_delta = max(dmg.tyre_wear) - max(prev_lap.tyre_wear)
                if current_wear_delta > (wear_rate * 1.5):
                    excess_deg = True

        rec = LapRecord(
            lap=self._lap_num, lap_time_ms=me.last_lap_ms, invalid=self._lap_invalid,
            position=me.position, ahead_idx=ahead_idx, gap_ahead_ms=gap_ahead,
            behind_idx=behind_idx, gap_behind_ms=gap_behind,
            fuel_kg=st.fuel_in_tank if st else None,
            tyre_wear=dmg.tyre_wear if dmg else None, stint=me.num_pit_stops,
            sector1_ms=s1, sector2_ms=s2, sector3_ms=s3,
            total_warnings=me.total_warnings,
            corner_cutting_warnings=me.corner_cutting_warnings,
            grid_position=me.grid_position,
            traffic_pct=traffic_pct,
            had_safety_car=self._had_safety_car_this_lap,
            new_damage=self._new_damage_this_lap,
            pit_status=me.pit_status,
            weather_changed=self._weather_changed_this_lap,
            blunder_moment=self._blunder_moment_this_lap,
            excess_deg=excess_deg,
            car_idx=self.player if self.player is not None else 0)

        lap_signals = [("lap", rec)]
        if not self._lap_invalid and s3 > 0:
            if self.best_sectors_session[2] == 0 or s3 <= self.best_sectors_session[2]:
                self.best_sectors_session[2] = s3
                lap_signals.append(("purple_sector", 3))

        self.laps.append(rec)
        self._lap_num, self._lap_invalid = me.lap_num, me.lap_invalid
        self._corner_cutting_warnings = me.corner_cutting_warnings

        # Reset per-lap accumulators
        self._lap_start_time = now
        self._last_traffic_sample_time = now
        self._lap_traffic_time_s = 0.0
        self._had_safety_car_this_lap = False
        self._new_damage_this_lap = False
        self._damage_at_lap_start = self.damage_parts()
        self._weather_at_lap_start = self.session.weather if self.session else None
        self._weather_changed_this_lap = False
        self._blunder_moment_this_lap = None
        self._player_s1_evaluated = False
        self._player_s2_evaluated = False

        return lap_signals

    # ---- accessors -------------------------------------------------------
    def _mine(self, cars):
        if cars is None or self.player >= len(cars):
            return None
        return cars[self.player]

    @property
    def my_tel2(self) -> packets.CarTelemetry2 | None:
        return self._mine(self.tel2)

    @property
    def me(self):
        return self._mine(self.lap)

    @property
    def track_length(self) -> float:
        if self.session and getattr(self.session, "track_length_m", 0) > 0:
            return float(self.session.track_length_m)
        return getattr(self, "_track_length", 5000.0)

    @track_length.setter
    def track_length(self, val: float):
        self._track_length = float(val)

    @property
    def safety_car(self) -> int:
        if self.session and hasattr(self.session, "safety_car"):
            return self.session.safety_car
        return getattr(self, "_safety_car", 0)

    @safety_car.setter
    def safety_car(self, val: int):
        self._safety_car = int(val)

    @property
    def my_status(self):
        return self._mine(self.status)

    @property
    def my_damage(self):
        return self._mine(self.damage)

    @property
    def my_tel(self):
        return self._mine(self.tel)

    @property
    def my_telemetry(self):
        return self._mine(self.tel)

    @property
    def my_motion(self):
        return self._mine(self.motion)

    @property
    def ptt(self) -> bool:
        return bool(self._buttons & self.ptt_mask)

    @property
    def ptt_down(self) -> bool:
        return self.ptt

    @property
    def my_setup(self):
        return self._mine(self.setups)

    @property
    def my_participant(self):
        return self.participants.get(self.player)

    @property
    def my_team_id(self):
        p = self.my_participant
        return p.team_id if p else 0

    def name(self, idx):
        n = self.names.get(idx)
        return n.split()[-1].title() if n else f"car {idx}"

    def car_in_position(self, pos):
        for i, c in enumerate(self.lap or ()):
            if c.position == pos and c.result_status in RUNNING:
                return i
        return None

    def gap_ahead(self):
        me = self.me
        if me is None or me.position <= 1:
            return None, None
        idx = self.car_in_position(me.position - 1)
        return (idx, me.gap_to_car_ahead_ms) if idx is not None else (None, None)

    def gap_behind(self):
        me = self.me
        if me is None:
            return None, None
        idx = self.car_in_position(me.position + 1)
        return (idx, self.lap[idx].gap_to_car_ahead_ms) if idx is not None else (None, None)

    def gap_trend(self, side: str):
        """Change in gap over the last lap, ms. Negative means the gap shrank."""
        if len(self.laps) < 2:
            return None
        a, b = self.laps[-2], self.laps[-1]
        if side == "ahead":
            ia, ga, ib, gb = a.ahead_idx, a.gap_ahead_ms, b.ahead_idx, b.gap_ahead_ms
        else:
            ia, ga, ib, gb = a.behind_idx, a.gap_behind_ms, b.behind_idx, b.gap_behind_ms
        if ia is None or ia != ib or ga is None or gb is None:
            return None
        return gb - ga

    def fuel_per_lap(self, n: int = 3):
        vals = [r.fuel_kg for r in list(self.laps)[-(n + 1):] if r.fuel_kg is not None]
        burns = [a - b for a, b in zip(vals, vals[1:]) if a > b]
        return sum(burns) / len(burns) if burns else None

    def _realtime_wear_per_lap(self) -> float | None:
        """Estimates tyre degradation rate (%/lap) in real-time before 2 full laps are finished."""
        dmg, st, me, s = self.my_damage, self.my_status, self.me, self.session
        if not dmg or not dmg.tyre_wear or not st:
            return None
        worst_wear = max(dmg.tyre_wear)
        age = float(getattr(st, "tyre_age_laps", 0))
        lap_dist = float(getattr(me, "lap_distance", 0.0)) if me else 0.0
        track_len = float(getattr(s, "track_length_m", 0.0)) if s else 0.0
        frac = (lap_dist / track_len) if (track_len > 0 and lap_dist > 0) else 0.0
        effective_laps = age + min(1.0, max(0.0, frac))

        if worst_wear > 0.05 and effective_laps >= 0.2:
            return round(worst_wear / effective_laps, 2)
        if age >= 1.0 and worst_wear > 0.0:
            return round(worst_wear / age, 2)

        comp_str = COMPOUNDS.get(getattr(st, "visual_compound", None), "")
        base_comp = comp_str.rstrip("s").lower()
        priors = {"soft": 4.2, "medium": 2.7, "hard": 1.7, "inter": 2.0, "wet": 1.5}
        for k, v in priors.items():
            if k in base_comp:
                return v
        return None

    def wear_per_lap(self):
        if self.laps:
            stint = self.laps[-1].stint
            recs = [r for r in self.laps if r.stint == stint and r.tyre_wear]
            if len(recs) >= 2 and recs[-1].lap != recs[0].lap:
                return (max(recs[-1].tyre_wear) - max(recs[0].tyre_wear)) / (recs[-1].lap - recs[0].lap)
            elif len(recs) == 1 and recs[0].tyre_wear and recs[0].lap > 0:
                return max(recs[0].tyre_wear) / max(1.0, float(recs[0].lap))
        return self._realtime_wear_per_lap()

    def player_rolling_pace(self, n: int = 3) -> float | None:
        """Returns average lap time in seconds of the player's last n valid clean laps."""
        valid_laps = [r.lap_time_s for r in self.laps if not r.invalid and r.lap_time_s > 0]
        if not valid_laps:
            return None
        recent = valid_laps[-n:]
        return sum(recent) / len(recent)

    def car_last_lap_s(self, idx: int) -> float | None:
        """Returns the last completed lap time in seconds for the given car index."""
        if not self.lap or idx >= len(self.lap):
            return None
        ms = getattr(self.lap[idx], "last_lap_ms", 0)
        return (ms / 1000.0) if ms > 0 else None

    def car_rolling_pace(self, idx: int, n: int = 3) -> float | None:
        """Returns rolling average lap time in seconds for a specific car."""
        history = self.car_laps_history.get(idx, [])
        valid_times = [l["lap_time_s"] for l in history if l.get("valid", True) and l.get("lap_time_s", 0) > 0]
        if valid_times:
            recent = valid_times[-n:]
            return sum(recent) / len(recent)
        return self.car_last_lap_s(idx)

    def battle_summary(self, max_gap_s: float = 4.0) -> dict:
        """Returns battle pace and gap analysis for player and rivals within max_gap_s."""
        my_last = self.laps[-1].lap_time_s if self.laps and self.laps[-1].lap_time_s > 0 else None
        if my_last is None and self.player is not None:
            my_last = self.car_last_lap_s(self.player)
        my_pace = self.player_rolling_pace(n=3) or my_last

        summary = {
            "my_last_lap_s": my_last,
            "my_avg_pace_s": my_pace,
            "ahead": None,
            "behind": None,
        }

        # Car ahead
        idx_ahead, ms_ahead = self.gap_ahead()
        if idx_ahead is not None and ms_ahead is not None:
            gap_s = ms_ahead / 1000.0
            last_s = self.car_last_lap_s(idx_ahead)
            pace_s = self.car_rolling_pace(idx_ahead, n=3)
            delta_s = None
            if my_pace and pace_s:
                delta_s = round(pace_s - my_pace, 3)
            elif my_last and last_s:
                delta_s = round(last_s - my_last, 3)

            pos_ahead = getattr(self.lap[idx_ahead], "position", 0) if self.lap and idx_ahead < len(self.lap) else 0
            is_pitting_ahead = bool(getattr(self.lap[idx_ahead], "pit_status", 0) != 0) if self.lap and idx_ahead < len(self.lap) else False
            summary["ahead"] = {
                "car_idx": idx_ahead,
                "name": self.name(idx_ahead),
                "position": pos_ahead,
                "gap_s": round(gap_s, 2),
                "last_lap_s": round(last_s, 3) if last_s else None,
                "pace_s": round(pace_s, 3) if pace_s else None,
                "delta_s": delta_s,
                "is_fighting": bool(gap_s <= max_gap_s),
                "is_pitting": is_pitting_ahead,
                "tyre_compound": COMPOUNDS.get(getattr(self.status[idx_ahead], "visual_compound", 0), "unknown") if (self.status and idx_ahead < len(self.status)) else None,
                "tyre_age_laps": getattr(self.status[idx_ahead], "tyre_age_laps", None) if (self.status and idx_ahead < len(self.status)) else None,
            }

        # Car behind
        idx_behind, ms_behind = self.gap_behind()
        if idx_behind is not None and ms_behind is not None:
            gap_s = ms_behind / 1000.0
            last_s = self.car_last_lap_s(idx_behind)
            pace_s = self.car_rolling_pace(idx_behind, n=3)
            delta_s = None
            if my_pace and pace_s:
                delta_s = round(my_pace - pace_s, 3)
            elif my_last and last_s:
                delta_s = round(my_last - last_s, 3)

            pos_behind = getattr(self.lap[idx_behind], "position", 0) if self.lap and idx_behind < len(self.lap) else 0
            is_pitting_behind = bool(getattr(self.lap[idx_behind], "pit_status", 0) != 0) if self.lap and idx_behind < len(self.lap) else False
            summary["behind"] = {
                "car_idx": idx_behind,
                "name": self.name(idx_behind),
                "position": pos_behind,
                "gap_s": round(gap_s, 2),
                "last_lap_s": round(last_s, 3) if last_s else None,
                "pace_s": round(pace_s, 3) if pace_s else None,
                "delta_s": delta_s,
                "is_fighting": bool(gap_s <= max_gap_s),
                "is_pitting": is_pitting_behind,
                "tyre_compound": COMPOUNDS.get(getattr(self.status[idx_behind], "visual_compound", 0), "unknown") if (self.status and idx_behind < len(self.status)) else None,
                "tyre_age_laps": getattr(self.status[idx_behind], "tyre_age_laps", None) if (self.status and idx_behind < len(self.status)) else None,
            }

        return summary

    def retirements_summary(self) -> dict:
        """Returns summary of all DNFs/retirements in the session."""
        running = len([c for c in (self.lap or []) if getattr(c, "result_status", 0) in RUNNING])
        return {
            "total_retired": len(self.retirements),
            "cars_running": running,
            "retired_drivers": [r["name"] for r in self.retirements],
            "retirements": list(self.retirements),
            "latest": self.retirements[-1] if self.retirements else None,
        }


    def next_rain(self, min_pct: int = 30):
        if not self.session:
            return None
        return next((f for f in self.session.forecast if f.minutes_ahead > 0 and f.rain_pct >= min_pct), None)

    def damage_parts(self) -> dict:
        d = self.my_damage
        if not d:
            return {}
        parts = {"front left wing": d.front_left_wing, "front right wing": d.front_right_wing,
                 "rear wing": d.rear_wing, "floor": d.floor, "diffuser": d.diffuser,
                 "sidepod": d.sidepod, "gearbox": d.gearbox, "engine": d.engine}
        return {k: v for k, v in parts.items() if v > 0}

    def timing_tower(self) -> list[dict]:
        """Returns all running cars ordered by position with name, team id,
        gap to car ahead, gap to leader, last lap, tyre compound and age, pit stops,
        and penalties."""
        if not self.lap:
            return []
        cars = []
        for i, c in enumerate(self.lap):
            if c.result_status in RUNNING and c.position > 0:
                part = self.participants.get(i)
                name = self.name(i)
                team_id = part.team_id if part else None
                st = self.status[i] if self.status and i < len(self.status) else None
                compound = COMPOUNDS.get(st.visual_compound, "unknown") if st else None
                tyre_age = st.tyre_age_laps if st else None
                cars.append({
                    "position": c.position,
                    "car_idx": i,
                    "name": name,
                    "team_id": team_id,
                    "gap_to_ahead": _s(c.gap_to_car_ahead_ms),
                    "gap_ahead": _s(c.gap_to_car_ahead_ms),
                    "gap_to_ahead_s": _s(c.gap_to_car_ahead_ms),
                    "gap_to_leader": _s(c.gap_to_leader_ms),
                    "gap_leader": _s(c.gap_to_leader_ms),
                    "gap_to_leader_s": _s(c.gap_to_leader_ms),
                    "last_lap": _s(c.last_lap_ms),
                    "last_lap_s": _s(c.last_lap_ms),
                    "best_lap_s": self.get_car_best_lap_s(i),
                    "current_lap_s": _s(c.current_lap_ms) if hasattr(c, "current_lap_ms") and c.current_lap_ms > 0 else None,
                    "tyre_compound": compound,
                    "compound": compound,
                    "tyre_age": tyre_age,
                    "tyre_age_laps": tyre_age,
                    "pit_stops": c.num_pit_stops,
                    "is_in_pit": bool(getattr(c, "pit_status", 0) in (1, 2)),
                    "penalties": c.penalties_s,
                    "penalties_s": c.penalties_s,
                    "is_player": (i == self.player),
                    "color": getattr(part, "primary_color_hex", None) if part else None,
                })
        cars.sort(key=lambda x: x["position"])
        return cars
    def teammate_idx(self) -> int | None:
        """Index of the player's teammate in the session, or None."""
        if self.player is None or not self.participants:
            return None
        tid = self.my_team_id
        if not tid:
            return None
        for i, p in self.participants.items():
            if i != self.player and p.team_id == tid:
                return i
        return None

    def teammate_name(self) -> str | None:
        """Surname of the player's teammate, or None."""
        idx = self.teammate_idx()
        if idx is None:
            return None
        return self.name(idx)

    def get_car_best_lap_s(self, car_idx: int | None) -> float | None:
        """Returns best lap time in seconds for a car in this session, or None."""
        if car_idx is None:
            return None
        ms = self.best_laps_all_cars.get(car_idx)
        return round(ms / 1000.0, 3) if ms else None

    def gap_to_pole_s(self) -> float | None:
        """Gap in seconds between player's best lap and session fastest lap (pole)."""
        if not self.best_laps_all_cars:
            return None
        my_best = self.best_laps_all_cars.get(self.player)
        if not my_best:
            return None
        pole_ms = min(self.best_laps_all_cars.values())
        return round((my_best - pole_ms) / 1000.0, 3)

    def quali_position(self) -> int | None:
        """Player's current qualifying timesheet position."""
        if not self.me:
            return None
        return self.me.position

    def elimination_cutoff(self, eliminated_per_segment: int | None = None) -> int | None:
        """Calculates elimination cutoff position for current qualifying segment."""
        s = self.session
        if not s:
            return None
        stype = s.session_type
        # Q1 = 5, Q2 = 6, Q3 = 7, Short = 8, One-shot = 9
        if stype == 5:  # Q1
            running = len([c for c in (self.lap or ()) if c.result_status in RUNNING])
            if running == 0:
                running = 20
            if eliminated_per_segment is not None:
                elim = eliminated_per_segment
            else:
                elim = 6 if running >= 22 else 5
            return max(1, running - elim)
        elif stype == 6:  # Q2
            return 10
        return None

    def speed_trap_ranking(self) -> tuple[float, int, int, str | None]:
        """Returns (speed_kph, rank, total_cars, hint)."""
        my_speed = self.speed_traps_all_cars.get(self.player, 0.0)
        if not self.speed_traps_all_cars or my_speed <= 0.0:
            return (0.0, 0, 0, None)
        sorted_speeds = sorted(self.speed_traps_all_cars.values(), reverse=True)
        total_cars = len(sorted_speeds)
        try:
            rank = sorted_speeds.index(my_speed) + 1
        except ValueError:
            rank = total_cars
        hint = None
        top_third_cutoff = math.ceil(total_cars / 3.0) if total_cars > 0 else 1
        if rank <= top_third_cutoff:
            losing_time = False
            if self.last_lap_vs_previous and self.last_lap_vs_previous.get("slower"):
                losing_time = True
            elif self.last_lap_vs_best and self.last_lap_vs_best.get("delta_s", 0) > 0.3:
                losing_time = True
            elif self.me and self.me.position > (self.expected_performance.get("position") or 5):
                losing_time = True
            if losing_time:
                hint = "car's quick on straights, you're losing in corners"
        return (my_speed, rank, total_cars, hint)

    # ---- compact state for Redis / the LLM prompt ------------------------
    def snapshot(self) -> dict:
        me, s, st, dmg, tel, tel2 = self.me, self.session, self.my_status, self.my_damage, self.my_tel, self.my_tel2
        regs_2026 = (tel2.regs_2026 if tel2 else False) or (getattr(s, "formula", 0) == 13)
        ai, ga = self.gap_ahead()
        bi, gb = self.gap_behind()
        rain = self.next_rain()
        fpl, wpl = self.fuel_per_lap(), self.wear_per_lap()

        st_spd, st_rank, st_total, st_hint = self.speed_trap_ranking()
        t_idx = self.teammate_idx()
        t_name = self.teammate_name()
        t_best = self.get_car_best_lap_s(t_idx)
        my_best = self.get_car_best_lap_s(self.player)
        if self.personal_best is None and my_best is not None:
            self.personal_best = my_best
        t_gap = round((my_best - t_best), 3) if (my_best and t_best) else None

        raw_stype = getattr(s, "session_type", 0) if s else 0
        try:
            stype = int(raw_stype)
        except (ValueError, TypeError):
            stype = 0
        stype_name = SESSION_TYPES.get(stype, "Unknown") if s else "Standby"
        is_practice = bool(s and 1 <= stype <= 4)
        is_quali = bool(s and 5 <= stype <= 9)
        is_race = bool(s and 10 <= stype <= 12)
        is_time_trial = bool(s and stype == 13)

        raw_time_left = getattr(s, "time_left_s", None) if s else None
        try:
            time_left_s = int(raw_time_left) if raw_time_left is not None else None
        except (ValueError, TypeError):
            time_left_s = None

        current_lap_s = _s(me.current_lap_ms) if (me and hasattr(me, "current_lap_ms") and isinstance(me.current_lap_ms, (int, float)) and me.current_lap_ms > 0) else None
        sector1_s = _s(me.sector1_ms) if (me and hasattr(me, "sector1_ms") and isinstance(me.sector1_ms, (int, float)) and me.sector1_ms > 0) else None
        sector2_s = _s(me.sector2_ms) if (me and hasattr(me, "sector2_ms") and isinstance(me.sector2_ms, (int, float)) and me.sector2_ms > 0) else None
        session_best_ms = min(self.best_laps_all_cars.values()) if self.best_laps_all_cars else None
        session_best_s = round(session_best_ms / 1000.0, 3) if session_best_ms else None

        return {
            "track": TRACKS.get(s.track_id, f"track {s.track_id}") if s else None,
            "session_type": stype,
            "session_type_name": stype_name,
            "session_time_left_s": time_left_s,
            "is_practice": is_practice,
            "is_quali": is_quali,
            "is_race": is_race,
            "is_time_trial": is_time_trial,
            "lap": me.lap_num if me else None,
            "total_laps": s.total_laps if s else None,
            "position": me.position if me else None,
            "grid_position": getattr(me, "grid_position", None) if me else None,
            "last_lap_s": _s(me.last_lap_ms) if me else None,
            "current_lap_s": current_lap_s,
            "best_lap_s": my_best,
            "session_best_s": session_best_s,
            "sector1_s": sector1_s,
            "sector2_s": sector2_s,
            "pit_stops": me.num_pit_stops if me else None,
            "car_ahead": {
                "name": self.name(ai),
                "gap_s": _s(ga),
                "trend_s_per_lap": _s(self.gap_trend("ahead")),
                "tyre_compound": COMPOUNDS.get(getattr(self.status[ai], "visual_compound", 0), "unknown") if (self.status and ai < len(self.status)) else None,
                "tyre_age_laps": getattr(self.status[ai], "tyre_age_laps", None) if (self.status and ai < len(self.status)) else None,
            } if ai is not None else None,
            "car_behind": {
                "name": self.name(bi),
                "gap_s": _s(gb),
                "trend_s_per_lap": _s(self.gap_trend("behind")),
                "tyre_compound": COMPOUNDS.get(getattr(self.status[bi], "visual_compound", 0), "unknown") if (self.status and bi < len(self.status)) else None,
                "tyre_age_laps": getattr(self.status[bi], "tyre_age_laps", None) if (self.status and bi < len(self.status)) else None,
            } if bi is not None else None,
            "fuel": {"kg": round(st.fuel_in_tank, 1), "laps_delta": round(st.fuel_laps_delta, 2),
                     "kg_per_lap": round(fpl, 2) if fpl else None} if st else None,
            "tyres": {"compound": COMPOUNDS.get(st.visual_compound, "unknown"), "age_laps": st.tyre_age_laps,
                      "wear_pct": dict(zip(TYRES, (round(w) for w in dmg.tyre_wear))) if dmg else None,
                      "wear_pct_per_lap": round(wpl, 1) if wpl else None,
                      "surface_temp_c": dict(zip(TYRES, tel.tyre_surface_temps)) if tel else None}
            if st else None,
            "ers_pct": round(100 * st.ers_store_j / ERS_MAX_J) if st else None,
            "damage_pct": self.damage_parts(),
            "weather": {"now": WEATHER.get(s.weather), "track_c": s.track_temp, "air_c": s.air_temp,
                        "next_rain": {"in_min": rain.minutes_ahead, "pct": rain.rain_pct} if rain else None}
            if s else None,
            "safety_car": SAFETY_CAR.get(s.safety_car) if s else None,
            "safety_car_status": s.safety_car if s else 0,
            "marshal_zones": [{"start": mz[0], "flag": mz[1]} for mz in s.marshal_zones] if (s and hasattr(s, "marshal_zones")) else [],
            "drs_zones": [{"start": dz[0], "end": dz[1]} for dz in s.drs_zones] if (s and hasattr(s, "drs_zones")) else [],
            "pit_window": {"ideal_lap": s.pit_ideal_lap, "latest_lap": s.pit_latest_lap}
            if s and s.pit_ideal_lap else None,
            "personal_best": self.personal_best,
            "last_lap_vs_best": self.last_lap_vs_best,
            "my_theoretical_best": self.my_theoretical_best,
            "biggest_loss_segment": self.biggest_loss_segment,
            "fastest_sectors": self.fastest_sectors,
            "setup": {
                "current": {
                    "id": self.current_setup_id,
                    "fingerprint": self.current_setup_fingerprint,
                    "params": self.my_setup.to_dict() if self.my_setup else None,
                } if self.my_setup else None,
                "best": self.best_setup_info,
            } if (self.my_setup or self.best_setup_info) else None,

            # Pace & expectation snapshot additions (Prompt 18)
            "gap_to_pole": self.gap_to_pole_s(),
            "quali_position": self.quali_position(),
            "elimination_cutoff": self.elimination_cutoff(),
            "teammate": {
                "name": t_name,
                "best_lap_s": t_best,
                "gap_s": t_gap,
            } if t_idx is not None else None,
            "expected_position": self.expected_performance,
            "last_lap_vs_previous": self.last_lap_vs_previous,
            "battle_pace": self.battle_summary(),
            "retirements": self.retirements_summary(),
            "strategy": getattr(self, "strategy_summary", None),
            "speed_trap": {
                "speed_kph": st_spd,
                "rank": st_rank,
                "total_cars": st_total,
                "hint": st_hint,
            } if st_spd > 0 else None,
            "regs_2026": regs_2026,
            "active_aero": {
                "mode": ("straight" if tel2.active_aero_mode == 1 else "corner") if tel2 else "corner",
                "mode_id": tel2.active_aero_mode if tel2 else 0,
                "available": tel2.active_aero_available if tel2 else False,
                "distance_m": tel2.active_aero_in_m if tel2 else 0,
            } if regs_2026 and tel2 else None,
            "overtake_mode": {
                "available": tel2.overtake_available if tel2 else False,
                "active": tel2.overtake_active if tel2 else False,
                "distance_m": tel2.overtake_in_m if tel2 else 0,
            } if regs_2026 and tel2 else None,
            "drs": (
                {"allowed": tel2.active_aero_available, "open": (tel2.active_aero_mode == 1)}
                if regs_2026 and tel2 else
                {"allowed": getattr(st, "drs_allowed", False) if st else False, "open": getattr(tel, "drs", False) if tel else False}
            ),
            "tyre_sets": [
                {
                    "actual_compound": ts.actual_compound,
                    "usable_life": ts.usable_life,
                    "wear_pct": ts.wear,
                    "lap_delta_s": round(ts.lap_delta_ms / 1000.0, 3),
                    "fitted": ts.fitted,
                }
                for ts in self.tyre_sets.sets
            ] if self.tyre_sets else None,
        }

    @property
    def is_practice(self) -> bool:
        s = self.session
        return bool(s and 1 <= getattr(s, "session_type", 0) <= 4)

    @property
    def is_quali(self) -> bool:
        s = self.session
        return bool(s and 5 <= getattr(s, "session_type", 0) <= 9)

    @property
    def is_race(self) -> bool:
        s = self.session
        stype = getattr(s, "session_type", 0) if s else 0
        if not s:
            return False
        return bool(10 <= stype <= 12 or (stype not in range(1, 10) and stype != 13))

    def get_points(self, position: int | None = None) -> int:
        """Returns official F1 championship points awarded for this session.
        Practice, Qualifying and Time Trial award 0 points.
        Race awards: P1=25, P2=18, P3=15, P4=12, P5=10, P6=8, P7=6, P8=4, P9=2, P10=1, P11+=0.
        """
        if self.is_practice or self.is_quali:
            return 0
        s = self.session
        stype = getattr(s, "session_type", 0) if s else 0
        if stype == 13:
            return 0
        pos = position if position is not None else (self.me.position if self.me else None)
        if pos is None:
            return 0
        return F1_RACE_POINTS.get(pos, 0)


F1_RACE_POINTS = {
    1: 25,
    2: 18,
    3: 15,
    4: 12,
    5: 10,
    6: 8,
    7: 6,
    8: 4,
    9: 2,
    10: 1,
}
