"""Live race state built from parsed packets, plus the derived numbers an engineer uses."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from . import packets
from .packets import PacketId, udp_action
from setups import compute_setup_fingerprint

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
        self.current_setup_id: str | int | None = None
        self.current_setup_fingerprint: str | None = None
        self.best_setup_info: dict | None = None
        self.names: dict[int, str] = {}
        self.participants: dict[int, packets.Participant] = {}
        self.last_packet_time = 0.0
        self.laps: deque[LapRecord] = deque(maxlen=self._history)
        self._lap_num = None
        self._lap_invalid = False
        self._corner_cutting_warnings = None
        self._buttons = 0
        self.personal_best = None
        self.last_lap_vs_best = None

    # ---- ingestion -------------------------------------------------------
    def update(self, h: packets.Header, data) -> list[tuple]:
        """Apply one parsed packet. Returns signals: ('lap', rec), ('ptt', bool),
        ('event', Event), ('damage',), ('new_session',), ('flashback', lap_num),
        ('lap_invalidated',), ('track_limits_warning', n), ('penalty', Event)."""
        signals = []
        self.last_packet_time = time.monotonic()
        if h.session_uid != self.session_uid:
            self._reset(h.session_uid)
            self.last_packet_time = time.monotonic()
            signals.append(("new_session",))
        self.player = h.player_idx
        pid = h.packet_id
        if pid == PacketId.MOTION:
            self.motion = data
        elif pid == PacketId.LAP_DATA:
            self.lap = data
            signals += self._check_lap()
        elif pid == PacketId.CAR_TELEMETRY:
            self.tel = data
        elif pid == PacketId.CAR_STATUS:
            self.status = data
        elif pid == PacketId.CAR_DAMAGE:
            self.damage = data
            signals.append(("damage",))
        elif pid == PacketId.SESSION:
            self.session = data
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
        elif pid == PacketId.EVENT:
            if data.code == "BUTN":
                was = bool(self._buttons & self.ptt_mask)
                now = bool(data.buttons & self.ptt_mask)
                self._buttons = data.buttons
                if now != was:
                    signals.append(("ptt", now))
            elif data.code == "PENA" and data.vehicle_idx == self.player:
                signals.append(("penalty", data))
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
            return signals

        ahead_idx, gap_ahead = self.gap_ahead()
        behind_idx, gap_behind = self.gap_behind()
        st, dmg = self.my_status, self.my_damage
        s1 = me.sector1_ms
        s2 = me.sector2_ms
        s3 = max(0, me.last_lap_ms - s1 - s2) if (me.last_lap_ms > 0 and s1 > 0 and s2 > 0) else 0
        rec = LapRecord(
            lap=self._lap_num, lap_time_ms=me.last_lap_ms, invalid=self._lap_invalid,
            position=me.position, ahead_idx=ahead_idx, gap_ahead_ms=gap_ahead,
            behind_idx=behind_idx, gap_behind_ms=gap_behind,
            fuel_kg=st.fuel_in_tank if st else None,
            tyre_wear=dmg.tyre_wear if dmg else None, stint=me.num_pit_stops,
            sector1_ms=s1, sector2_ms=s2, sector3_ms=s3,
            total_warnings=me.total_warnings,
            corner_cutting_warnings=me.corner_cutting_warnings)
        self.laps.append(rec)
        self._lap_num, self._lap_invalid = me.lap_num, me.lap_invalid
        self._corner_cutting_warnings = me.corner_cutting_warnings
        return [("lap", rec)]

    # ---- accessors -------------------------------------------------------
    def _mine(self, cars):
        if cars is None or self.player >= packets.NUM_CARS:
            return None
        return cars[self.player]

    @property
    def me(self):
        return self._mine(self.lap)

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

    def wear_per_lap(self):
        if not self.laps:
            return None
        stint = self.laps[-1].stint
        recs = [r for r in self.laps if r.stint == stint and r.tyre_wear]
        if len(recs) < 2 or recs[-1].lap == recs[0].lap:
            return None
        return (max(recs[-1].tyre_wear) - max(recs[0].tyre_wear)) / (recs[-1].lap - recs[0].lap)

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
                    "tyre_compound": compound,
                    "compound": compound,
                    "tyre_age": tyre_age,
                    "tyre_age_laps": tyre_age,
                    "pit_stops": c.num_pit_stops,
                    "penalties": c.penalties_s,
                    "penalties_s": c.penalties_s,
                    "is_player": (i == self.player),
                })
        cars.sort(key=lambda x: x["position"])
        return cars

    # ---- compact state for Redis / the LLM prompt ------------------------
    def snapshot(self) -> dict:
        me, s, st, dmg, tel = self.me, self.session, self.my_status, self.my_damage, self.my_tel
        ai, ga = self.gap_ahead()
        bi, gb = self.gap_behind()
        rain = self.next_rain()
        fpl, wpl = self.fuel_per_lap(), self.wear_per_lap()
        return {
            "track": TRACKS.get(s.track_id, f"track {s.track_id}") if s else None,
            "lap": me.lap_num if me else None,
            "total_laps": s.total_laps if s else None,
            "position": me.position if me else None,
            "last_lap_s": _s(me.last_lap_ms) if me else None,
            "pit_stops": me.num_pit_stops if me else None,
            "car_ahead": {"name": self.name(ai), "gap_s": _s(ga), "trend_s_per_lap": _s(self.gap_trend("ahead"))}
            if ai is not None else None,
            "car_behind": {"name": self.name(bi), "gap_s": _s(gb), "trend_s_per_lap": _s(self.gap_trend("behind"))}
            if bi is not None else None,
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
            "pit_window": {"ideal_lap": s.pit_ideal_lap, "latest_lap": s.pit_latest_lap}
            if s and s.pit_ideal_lap else None,
            "personal_best": self.personal_best,
            "last_lap_vs_best": self.last_lap_vs_best,
            "setup": {
                "current": {
                    "id": self.current_setup_id,
                    "fingerprint": self.current_setup_fingerprint,
                    "params": self.my_setup.to_dict() if self.my_setup else None,
                } if self.my_setup else None,
                "best": self.best_setup_info,
            } if (self.my_setup or self.best_setup_info) else None,
        }
